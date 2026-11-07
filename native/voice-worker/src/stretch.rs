//! Slowing fast speech down without changing its pitch (WSOLA: waveform-similarity
//! overlap-add). Whisper tiny mishears speech much faster than it was trained on
//! ("close this word" for a quick "close Discord"); the same words slowed to an
//! ordinary pace come out right.

const FRAME: usize = 480; // 30 ms at 16 kHz
const HOP_OUT: usize = FRAME / 2;
const TOLERANCE: usize = 120; // how far a frame may shift to line up with the last

/// 16-bit mono PCM made `factor` times longer (1.5: two thirds of the speed).
pub fn slower(pcm: &[u8], factor: f64) -> Vec<u8> {
    let mut input: Vec<f32> = pcm
        .chunks_exact(2)
        .map(|s| i16::from_le_bytes([s[0], s[1]]) as f32)
        .collect();
    if !(1.0..=3.0).contains(&factor) || input.len() < FRAME + 2 * TOLERANCE {
        return pcm.to_vec();
    }
    // Quiet past the end, so the last frames reach the final sound of the word.
    let wanted = (input.len() as f64 * factor).round() as usize;
    input.resize(input.len() + FRAME + 2 * TOLERANCE, 0.0);
    let window: Vec<f32> = (0..FRAME)
        .map(|i| 0.5 - 0.5 * (2.0 * std::f32::consts::PI * i as f32 / FRAME as f32).cos())
        .collect();
    let hop_in = HOP_OUT as f64 / factor;
    let length = (input.len() as f64 * factor) as usize + FRAME;
    let (mut output, mut weight) = (vec![0f32; length], vec![0f32; length]);
    let mut previous: Option<usize> = None;
    let mut frame = 0usize;
    loop {
        let nominal = (frame as f64 * hop_in) as usize;
        if nominal + FRAME + TOLERANCE > input.len() {
            break;
        }
        // Where this frame best continues the waveform the last one left off.
        let position = match previous {
            None => nominal,
            Some(last) => {
                let natural = &input[last + HOP_OUT..last + HOP_OUT + HOP_OUT];
                let low = nominal.saturating_sub(TOLERANCE);
                let high = (nominal + TOLERANCE).min(input.len() - FRAME);
                (low..=high)
                    .max_by(|&a, &b| {
                        let score = |p: usize| -> f32 {
                            natural.iter().zip(&input[p..p + HOP_OUT]).map(|(x, y)| x * y).sum()
                        };
                        score(a).total_cmp(&score(b))
                    })
                    .unwrap_or(nominal)
            }
        };
        let at = frame * HOP_OUT;
        for i in 0..FRAME {
            output[at + i] += input[position + i] * window[i];
            weight[at + i] += window[i];
        }
        previous = Some(position);
        frame += 1;
    }
    output[..wanted.min(length)]
        .iter()
        .zip(&weight)
        .flat_map(|(value, w)| {
            let sample = if *w > 1e-3 { value / w } else { 0.0 };
            (sample.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16).to_le_bytes()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::slower;

    fn tone(seconds: f64, hz: f64) -> Vec<u8> {
        (0..(seconds * 16000.0) as usize)
            .flat_map(|i| (((i as f64 * hz * 2.0 * std::f64::consts::PI / 16000.0).sin() * 8000.0) as i16).to_le_bytes())
            .collect()
    }

    fn crossings(pcm: &[u8]) -> usize {
        let samples: Vec<i16> = pcm.chunks_exact(2).map(|s| i16::from_le_bytes([s[0], s[1]])).collect();
        samples.windows(2).filter(|w| (w[0] < 0) != (w[1] < 0)).count()
    }

    #[test]
    fn speech_gets_longer_and_keeps_its_pitch() {
        let input = tone(1.0, 220.0);
        let output = slower(&input, 1.5);
        let ratio = output.len() as f64 / input.len() as f64;
        assert!((1.4..1.6).contains(&ratio), "{ratio}");
        // Same pitch: zero crossings per second stay about the same.
        let per_second = |pcm: &[u8]| crossings(pcm) as f64 / (pcm.len() as f64 / 32000.0);
        let (before, after) = (per_second(&input), per_second(&output));
        assert!((after / before - 1.0).abs() < 0.05, "{before} {after}");
    }

    #[test]
    fn nothing_to_stretch_is_left_alone() {
        assert_eq!(slower(&[1, 0, 2, 0], 1.5), vec![1, 0, 2, 0]);
        let input = tone(0.5, 300.0);
        assert_eq!(slower(&input, 1.0).len(), input.len());
        assert_eq!(slower(&input, 9.0), input); // out of range: unchanged
    }

    #[test]
    fn the_last_sound_is_kept() {
        // Quiet, then a burst right at the end: it must still be there, stretched.
        let mut input = vec![0u8; 16000];
        input.extend(tone(0.05, 500.0));
        let output = slower(&input, 1.5);
        assert_eq!(output.len(), (input.len() as f64 / 2.0 * 1.5).round() as usize * 2);
        let tail: Vec<i16> = output[output.len() - 1600..].chunks_exact(2).map(|s| i16::from_le_bytes([s[0], s[1]])).collect();
        assert!(tail.iter().any(|s| s.unsigned_abs() > 2000));
    }
}
