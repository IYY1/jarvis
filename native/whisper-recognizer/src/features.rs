//! The 16 kHz, 400-point periodic-Hann, 160-sample-shift Whisper front end.
//! Frame placement and symmetric boundary padding follow kaldi-native-fbank
//! 1.22.3, used by the existing sherpa-onnx recognizer (not torch's centering).
use rustfft::{num_complex::Complex32, Fft, FftPlanner};
use std::sync::Arc;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::ensure;

const FFT: usize = 400;
const HOP: usize = 160;
pub const MELS: usize = 80;
pub struct Features {
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    filters: Vec<Vec<(usize, f32)>>,
}

impl Default for Features {
    fn default() -> Self {
        let high_mel = 15.0 + 14.545_078_f32 * 8.0_f32.ln();
        let delta = high_mel / (MELS + 1) as f32;
        let hz = |m: f32| {
            if m <= 15.0 {
                200.0_f32 / 3.0 * m
            } else {
                1000.0 * ((m - 15.0) * 0.068_751_775_f32).exp()
            }
        };
        let filters = (0..MELS)
            .map(|i| {
                let left = hz(i as f32 * delta);
                let center = hz((i + 1) as f32 * delta);
                let right = hz((i + 2) as f32 * delta);
                (0..=FFT / 2)
                    .filter_map(|k| {
                        let f = k as f32 * 40.0;
                        if f > left && f < right {
                            let w = if f <= center {
                                (f - left) / (center - left)
                            } else {
                                (right - f) / (right - center)
                            };
                            Some((k, w * (2.0 / (right - left))))
                        } else {
                            None
                        }
                    })
                    .collect()
            })
            .collect();
        Self {
            fft: FftPlanner::new().plan_fft_forward(FFT),
            window: (0..FFT)
                .map(|i| (0.5 - 0.5 * (std::f64::consts::TAU * i as f64 / FFT as f64).cos()) as f32)
                .collect(),
            filters,
        }
    }
}
impl Features {
    /// Includes the application's 250 ms leading/trailing silence. Returns
    /// channel-major normalized mels with 1000 zero feature frames appended.
    pub fn compute(&self, samples: &[f32]) -> TractResult<(Tensor, usize)> {
        // The outer PCM boundary enforces 0.2 seconds. GTCRN rounds its output
        // down to a 256-sample hop, so its shortest valid result is 3072.
        ensure!(
            (256..=160000).contains(&samples.len()),
            "Expected 256–160000 audio samples"
        );
        ensure!(samples.iter().all(|x| x.is_finite()), "Non-finite audio");
        // Read the reference's silence padding virtually, and write directly
        // into the inference tensor instead of keeping two full mel buffers.
        let padded_len = samples.len() + 8000;
        let frames = (padded_len + HOP / 2) / HOP;
        let total = frames + 1000;
        let mut tensor = Tensor::zero::<f32>(&[1, MELS, total])?;
        let mut plain = tensor.try_as_plain_ram_mut()?;
        let mel = plain.as_slice_mut::<f32>()?;
        let mut window = vec![Complex32::default(); FFT];
        let mut scratch = vec![Complex32::default(); self.fft.get_inplace_scratch_len()];
        let mut power = [0.0; FFT / 2 + 1];
        let mut maximum = f32::NEG_INFINITY;
        for frame in 0..frames {
            let start = (frame * HOP) as isize - 120;
            for (i, sample) in window.iter_mut().enumerate() {
                let mut index = start + i as isize;
                if index < 0 {
                    index = -index - 1;
                }
                if index >= padded_len as isize {
                    index = 2 * padded_len as isize - 1 - index;
                }
                let value = (index as usize)
                    .checked_sub(4000)
                    .and_then(|i| samples.get(i))
                    .copied()
                    .unwrap_or(0.0);
                *sample = Complex32::new(value * self.window[i], 0.0);
            }
            self.fft.process_with_scratch(&mut window, &mut scratch);
            for (p, c) in power.iter_mut().zip(&window) {
                *p = c.re * c.re + c.im * c.im;
            }
            for (bin, filter) in self.filters.iter().enumerate() {
                let energy: f32 = filter.iter().map(|&(k, w)| power[k] * w).sum();
                let log = energy.max(1e-10).log10();
                maximum = maximum.max(log);
                mel[bin * total + frame] = log;
            }
        }
        for channel in mel.chunks_exact_mut(total) {
            for value in &mut channel[..frames] {
                *value = (value.max(maximum - 8.0) + 4.0) / 4.0;
            }
        }
        Ok((tensor, frames))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn silence_matches_reference_floor_and_tail_padding() -> TractResult<()> {
        let (mel, frames) = Features::default().compute(&vec![0.0; 3200])?;
        assert_eq!(frames, 70);
        assert_eq!(mel.shape(), [1, 80, 1070]);
        for bin in mel
            .to_plain_array_view::<f32>()?
            .as_slice()
            .unwrap()
            .as_chunks::<1070>()
            .0
        {
            assert!(bin[..70].iter().all(|&x| x == -1.5));
            assert!(bin[70..].iter().all(|&x| x == 0.0));
        }
        Ok(())
    }
    #[test]
    fn validates_samples_and_accepts_shortest_gtcrn_result() {
        let features = Features::default();
        assert!(features.compute(&[0.0; 3072]).is_ok());
        assert!(features.compute(&[]).is_err());
        assert!(features.compute(&vec![0.0; 160001]).is_err());
        assert!(features.compute(&[f32::NAN; 3200]).is_err());
    }
}
