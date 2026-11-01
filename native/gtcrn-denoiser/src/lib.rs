//! GTCRN denoising performed by Rust FFT, overlap-add and tract CPU kernels.
//! The ONNX file supplies the trained graph/weights, not an ONNX Runtime engine.
//! Spectral conventions match sherpa-onnx 1.13.8 / kaldi-native-fbank 1.22.3.
use rustfft::{num_complex::Complex32, Fft, FftPlanner};
use std::{collections::HashMap, path::Path, sync::Arc};
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

pub const SAMPLE_RATE: usize = 16000;
const FFT: usize = 512;
const HOP: usize = 256;
const BINS: usize = FFT / 2 + 1;
const MAX_SAMPLES: usize = SAMPLE_RATE * 10;
const STATE_SHAPES: [&[usize]; 3] = [&[2, 1, 16, 16, 33], &[2, 3, 1, 1, 16], &[2, 1, 33, 16]];

pub struct Denoiser {
    plan: Arc<TypedRunnableModel>,
    analysis: Arc<dyn Fft<f32>>,
    synthesis: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
}

impl Denoiser {
    /// Numerical diagnostic: run interleaved complex spectra directly through
    /// the Rust network, isolating neural execution from STFT/ISTFT.
    pub fn spectra(&self, input: &[f32]) -> TractResult<Vec<f32>> {
        ensure!(
            !input.is_empty() && input.len().is_multiple_of(BINS * 2),
            "Expected complete 257-bin complex frames"
        );
        ensure!(
            input.len() <= (1 + MAX_SAMPLES / HOP) * BINS * 2
                && input.iter().all(|x| x.is_finite()),
            "Invalid spectral input"
        );
        let mut states: TVec<TValue> = STATE_SHAPES
            .iter()
            .map(|s| Ok(Tensor::zero::<f32>(s)?.into_tvalue()))
            .collect::<TractResult<_>>()?;
        let mut output = Vec::with_capacity(input.len());
        for frame in input.as_chunks::<{ BINS * 2 }>().0 {
            let mut tensors = tvec!(Tensor::from_shape(&[1, BINS, 1, 2], frame)?.into_tvalue());
            tensors.append(&mut states);
            let mut result = self.plan.run(tensors)?;
            let enhanced = result.remove(0);
            output.extend(enhanced.to_plain_array_view::<f32>()?.iter().copied());
            states = result;
        }
        Ok(output)
    }
    pub fn load(path: impl AsRef<Path>) -> TractResult<Self> {
        let path = path.as_ref();
        ensure!(
            std::fs::metadata(path)?.len() <= 8 * 1024 * 1024,
            "GTCRN model exceeds 8 MiB"
        );
        let loader = tract_onnx::onnx();
        let proto = loader.proto_model_for_path(path)?;
        let metadata: HashMap<_, _> = proto
            .metadata_props
            .iter()
            .map(|p| (p.key.as_str(), p.value.as_str()))
            .collect();
        for (key, expected) in [
            ("model_type", "gtcrn"),
            ("version", "1"),
            ("sample_rate", "16000"),
            ("n_fft", "512"),
            ("hop_length", "256"),
            ("window_length", "512"),
            ("window_type", "hann_sqrt"),
        ] {
            ensure!(
                metadata.get(key).copied() == Some(expected),
                "Unsupported GTCRN metadata {key}: {:?}; expected {expected}",
                metadata.get(key)
            );
        }
        let graph = proto
            .graph
            .as_ref()
            .ok_or_else(|| format_err!("GTCRN graph missing"))?;
        ensure!(
            graph.input.iter().map(|v| v.name.as_str()).eq([
                "mix",
                "conv_cache",
                "tra_cache",
                "inter_cache"
            ]),
            "Unexpected GTCRN inputs"
        );
        ensure!(
            graph.output.iter().map(|v| v.name.as_str()).eq([
                "enh",
                "conv_cache_out",
                "tra_cache_out",
                "inter_cache_out"
            ]),
            "Unexpected GTCRN outputs"
        );
        let model = loader.model_for_proto_model(&proto)?.into_optimized()?;
        ensure!(
            model.input_outlets()?.len() == 4 && model.output_outlets()?.len() == 4,
            "GTCRN must have four inputs and outputs"
        );
        let plan = model.into_runnable()?;
        let mut planner = FftPlanner::<f32>::new();
        Ok(Self {
            plan,
            analysis: planner.plan_fft_forward(FFT),
            synthesis: planner.plan_fft_inverse(FFT),
            window: sqrt_hann(),
        })
    }

    /// Offline 16 kHz mono audio. Recurrent caches reset for every command,
    /// and survive between its spectral frames. Output length matches the
    /// reference: floor(input_length / 256) * 256 samples.
    pub fn process(&self, samples: &[f32]) -> TractResult<Vec<f32>> {
        ensure!(
            samples.len() > HOP && samples.len() <= MAX_SAMPLES,
            "Denoiser expects 257–160000 samples of 16 kHz audio"
        );
        ensure!(
            samples.iter().all(|x| x.is_finite()),
            "Audio contains a non-finite sample"
        );
        let frames = 1 + samples.len() / HOP;
        // Keep only the returned center crop. Every retained sample receives
        // two adjacent windows, so normalization needs one hop, not ten seconds.
        let mut output = vec![0.0_f32; (frames - 1) * HOP];
        let mut spectrum = vec![Complex32::default(); FFT];
        let mut input = vec![0.0_f32; BINS * 2];
        let mut scratch = vec![
            Complex32::default();
            self.analysis
                .get_inplace_scratch_len()
                .max(self.synthesis.get_inplace_scratch_len())
        ];
        let mut states: TVec<TValue> = STATE_SHAPES
            .iter()
            .map(|s| Ok(Tensor::zero::<f32>(s)?.into_tvalue()))
            .collect::<TractResult<_>>()?;
        for frame in 0..frames {
            for (i, value) in spectrum.iter_mut().enumerate() {
                let source = reflect(
                    frame as isize * HOP as isize + i as isize - HOP as isize,
                    samples.len(),
                );
                *value = Complex32::new(samples[source] * self.window[i], 0.0);
            }
            self.analysis
                .process_with_scratch(&mut spectrum, &mut scratch);
            for bin in 0..BINS {
                input[bin * 2] = spectrum[bin].re;
                input[bin * 2 + 1] = if bin == 0 || bin == FFT / 2 {
                    0.0
                } else {
                    spectrum[bin].im
                };
            }
            let mut tensors = tvec!(Tensor::from_shape(&[1, BINS, 1, 2], &input)?.into_tvalue());
            tensors.append(&mut states);
            // Each ONNX invocation starts fresh internal Scan/GRU state.
            // Only the three explicit cache tensors carry between frames.
            let mut result = self.plan.run(tensors)?;
            ensure!(
                result.len() == 4,
                "GTCRN returned an invalid number of tensors"
            );
            let enhanced = result.remove(0);
            let view = enhanced.to_plain_array_view::<f32>()?;
            let values = view
                .as_slice()
                .ok_or_else(|| format_err!("GTCRN spectrum is not contiguous"))?;
            ensure!(
                values.len() == BINS * 2 && values.iter().all(|x| x.is_finite()),
                "GTCRN returned invalid spectra"
            );
            for bin in 0..BINS {
                spectrum[bin] = Complex32::new(
                    values[bin * 2],
                    if bin == 0 || bin == FFT / 2 {
                        0.0
                    } else {
                        values[bin * 2 + 1]
                    },
                );
            }
            for bin in BINS..FFT {
                spectrum[bin] = spectrum[FFT - bin].conj();
            }
            states = result;
            self.synthesis
                .process_with_scratch(&mut spectrum, &mut scratch);
            overlap_add(&spectrum, &self.window, frame * HOP, &mut output);
        }
        normalize(&mut output, &self.window);
        Ok(output)
    }
}

fn sqrt_hann() -> Vec<f32> {
    (0..FFT)
        .map(|i| {
            ((0.5 - 0.5 * (std::f64::consts::TAU * i as f64 / FFT as f64).cos()) as f32).sqrt()
        })
        .collect()
}
fn reflect(mut index: isize, length: usize) -> usize {
    let last = length as isize - 1;
    while index < 0 || index > last {
        index = if index < 0 { -index } else { 2 * last - index };
    }
    index as usize
}
fn overlap_add(spectrum: &[Complex32], window: &[f32], offset: usize, output: &mut [f32]) {
    let start = HOP.saturating_sub(offset);
    let end = FFT.min((HOP + output.len()).saturating_sub(offset));
    for i in start..end {
        output[offset + i - HOP] += spectrum[i].re * (1.0 / FFT as f32) * window[i];
    }
}
fn normalize(output: &mut [f32], window: &[f32]) {
    let divisors: [f32; HOP] = std::array::from_fn(|i| {
        // Match the original overlap-add accumulation order exactly.
        window[i + HOP] * window[i + HOP] + window[i] * window[i]
    });
    for (i, sample) in output.iter_mut().enumerate() {
        if divisors[i % HOP] != 0.0 {
            *sample /= divisors[i % HOP];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reflection_excludes_the_endpoint() {
        assert_eq!(
            (0..4).map(|i| reflect(i - 4, 7)).collect::<Vec<_>>(),
            vec![4, 3, 2, 1]
        );
        assert_eq!(
            (7..11).map(|i| reflect(i, 7)).collect::<Vec<_>>(),
            vec![5, 4, 3, 2]
        );
    }
    #[test]
    fn spectral_round_trip_preserves_signal_and_reference_length() {
        let input: Vec<f32> = (0..1234).map(|i| (i as f32 * 0.031).sin() * 0.2).collect();
        let frames = 1 + input.len() / HOP;
        let window = sqrt_hann();
        let mut planner = FftPlanner::new();
        let forward = planner.plan_fft_forward(FFT);
        let inverse = planner.plan_fft_inverse(FFT);
        let mut output = vec![0.0; (frames - 1) * HOP];
        for frame in 0..frames {
            let mut spectrum: Vec<_> = (0..FFT)
                .map(|i| {
                    Complex32::new(
                        input[reflect((frame * HOP + i) as isize - HOP as isize, input.len())]
                            * window[i],
                        0.0,
                    )
                })
                .collect();
            forward.process(&mut spectrum);
            inverse.process(&mut spectrum);
            overlap_add(&spectrum, &window, frame * HOP, &mut output);
        }
        normalize(&mut output, &window);
        let result = &output;
        assert_eq!(result.len(), input.len() / HOP * HOP);
        assert!(result.iter().zip(input).all(|(a, b)| (a - b).abs() < 1e-6));
    }
    #[test]
    fn cropped_overlap_and_periodic_divisor_match_full_reference_bits() {
        let window = sqrt_hann();
        for frames in [2, 3, 5, 625, 626] {
            let total = FFT + (frames - 1) * HOP;
            let mut legacy = vec![0.0_f32; total];
            let mut divisors = vec![0.0_f32; total];
            let mut cropped = vec![0.0_f32; (frames - 1) * HOP];
            for frame in 0..frames {
                let spectrum: Vec<_> = (0..FFT)
                    .map(|i| Complex32::new(((frame * FFT + i) as f32 * 0.017).sin(), 0.0))
                    .collect();
                for i in 0..FFT {
                    legacy[frame * HOP + i] += spectrum[i].re * (1.0 / FFT as f32) * window[i];
                    divisors[frame * HOP + i] += window[i] * window[i];
                }
                overlap_add(&spectrum, &window, frame * HOP, &mut cropped);
            }
            for (sample, divisor) in legacy.iter_mut().zip(divisors) {
                if divisor != 0.0 {
                    *sample /= divisor;
                }
            }
            normalize(&mut cropped, &window);
            assert!(
                cropped
                    .iter()
                    .zip(&legacy[HOP..total - HOP])
                    .all(|(a, b)| a.to_bits() == b.to_bits()),
                "frames={frames}"
            );
        }
    }
}
