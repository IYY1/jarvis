//! Arabic speech recognition with Moonshine Streaming Tiny (MIT,
//! moonshine-ai/moonshine-streaming-tiny-ar) on tract kernels, without a
//! foreign inference runtime.
//!
//! The model is three ONNX graphs exported by scripts/export_moonshine_arabic.py:
//! the encoder (16 kHz samples to hidden states with the trained sliding-window
//! attention), cross (each decoder layer's cross-attention keys and values,
//! computed once per utterance) and decoder (tokens so far to next-token
//! logits). Decoding is greedy, capped at 6.5 tokens per second of audio as the
//! model card advises against repetition loops.
use std::{path::Path, sync::Arc};
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

const START: i64 = 1;
const END: usize = 2;
/// Samples per encoder frame; input is padded to a whole frame.
const FRAME: usize = 80;
/// Dimensions of each 40-wide attention head that rotary embedding turns
/// (partial_rotary_factor 0.8), with theta 10,000.
const ROTARY: usize = 32;
/// Longest audio one call accepts (the encoder handles longer, but dictation
/// segments and commands never need more).
pub const MAX_SAMPLES: usize = 30 * 16000;

type Plan = Arc<TypedRunnableModel>;

pub struct Recognizer {
    encoder: Plan,
    cross: Plan,
    decoder: Plan,
    pieces: Vec<Vec<u8>>,
}

fn plan(path: &Path, facts: impl Fn(&SymbolScope) -> Vec<InferenceFact>) -> TractResult<Plan> {
    let mut model = tract_onnx::onnx().model_for_path(path)?;
    let facts = facts(&model.symbols);
    for (i, fact) in facts.into_iter().enumerate() {
        model.set_input_fact(i, fact)?;
    }
    model.into_optimized()?.into_runnable()
}

impl Recognizer {
    /// `directory` holds encoder.onnx, cross.onnx, decoder.onnx and tokenizer.json.
    pub fn load(directory: impl AsRef<Path>) -> TractResult<Self> {
        let dir = directory.as_ref();
        let cache = |t: &Symbol| -> InferenceFact {
            f32::fact([6.to_dim(), 1.to_dim(), 8.to_dim(), t.to_dim(), 40.to_dim()]).into()
        };
        let encoder = plan(&dir.join("encoder.onnx"), |s| {
            vec![f32::fact([1.to_dim(), s.sym("f").to_dim(), FRAME.to_dim()]).into()]
        })?;
        let cross = plan(&dir.join("cross.onnx"), |s| {
            vec![f32::fact([1.to_dim(), s.sym("t").to_dim(), 320.to_dim()]).into()]
        })?;
        let decoder = plan(&dir.join("decoder.onnx"), |s| {
            let (t, l) = (s.sym("t"), s.sym("l"));
            let rope = || -> InferenceFact { f32::fact([l.to_dim(), ROTARY.to_dim()]).into() };
            vec![
                i64::fact([1.to_dim(), l.to_dim()]).into(),
                cache(&t),
                cache(&t),
                rope(),
                rope(),
            ]
        })?;
        let pieces = pieces(&std::fs::read(dir.join("tokenizer.json"))?)?;
        Ok(Self {
            encoder,
            cross,
            decoder,
            pieces,
        })
    }
    pub fn transcribe(&self, samples: &[f32]) -> TractResult<String> {
        ensure!(
            samples.len() <= MAX_SAMPLES,
            "Audio exceeds {} seconds",
            MAX_SAMPLES / 16000
        );
        if samples.len() < 1600 {
            return Ok(String::new());
        }
        let mut audio = samples.to_vec();
        audio.resize(samples.len().div_ceil(FRAME) * FRAME, 0.0);
        let memory = self
            .encoder
            .run(tvec!(Tensor::from_shape(
                &[1, audio.len() / FRAME, FRAME],
                &audio
            )?
            .into_tvalue()))?
            .remove(0);
        let mut kv = self.cross.run(tvec!(memory))?;
        // Plain owned copies: the decoder's matmuls pack them themselves.
        let plain = |t: TValue| -> TractResult<TValue> {
            let t = t.into_tensor();
            Ok(
                Tensor::from_shape(t.shape(), t.try_as_plain_ram()?.as_slice::<f32>()?)?
                    .into_tvalue(),
            )
        };
        let values = plain(kv.pop().unwrap())?;
        let keys = plain(kv.pop().unwrap())?;
        let limit = (samples.len() as f64 * 6.5 / 16000.0) as usize + 2;
        let mut tokens = vec![START];
        let (cos, sin) = rope(limit + 1);
        for _ in 0..limit {
            let n = tokens.len();
            let input = Tensor::from_shape(&[1, n], &tokens)?.into_tvalue();
            let cos = Tensor::from_shape(&[n, ROTARY], &cos[..n * ROTARY])?.into_tvalue();
            let sin = Tensor::from_shape(&[n, ROTARY], &sin[..n * ROTARY])?.into_tvalue();
            let causal: Vec<f32> = (0..n * n)
                .map(|i| if i % n > i / n { -1e9 } else { 0.0 })
                .collect();
            let causal = Tensor::from_shape(&[1, 1, n, n], &causal)?.into_tvalue();
            let logits = self
                .decoder
                .run(tvec!(input, keys.clone(), values.clone(), cos, sin, causal))?
                .remove(0);
            let logits = logits.try_as_plain_ram()?.as_slice::<f32>()?.to_vec();
            ensure!(logits.iter().all(|x| x.is_finite()), "Non-finite logits");
            let next = logits
                .iter()
                .enumerate()
                .fold(0, |best, (i, v)| if *v > logits[best] { i } else { best });
            if next == END {
                break;
            }
            tokens.push(next as i64);
        }
        Ok(decode(&self.pieces, &tokens[1..]))
    }
}

/// Rotary tables for positions 0..n, each frequency repeated for its pair:
/// [cos f0, cos f0, cos f1, cos f1, …], as the exported decoder expects.
fn rope(n: usize) -> (Vec<f32>, Vec<f32>) {
    let mut cos = Vec::with_capacity(n * ROTARY);
    let mut sin = Vec::with_capacity(n * ROTARY);
    for position in 0..n {
        for pair in 0..ROTARY / 2 {
            let frequency = 1.0 / 10000_f32.powf((2 * pair) as f32 / ROTARY as f32);
            let angle = position as f32 * frequency;
            cos.extend([angle.cos(); 2]);
            sin.extend([angle.sin(); 2]);
        }
    }
    (cos, sin)
}

/// SentencePiece-style pieces: "▁" is a space and "<0xNN>" a raw byte.
fn decode(pieces: &[Vec<u8>], tokens: &[i64]) -> String {
    let mut bytes = vec![];
    for &t in tokens {
        if let Some(piece) = pieces.get(t as usize) {
            bytes.extend_from_slice(piece);
        }
    }
    String::from_utf8_lossy(&bytes)
        .replace('\u{FFFD}', "")
        .trim()
        .to_owned()
}

/// Token bytes by ID from a Hugging Face tokenizer.json (BPE with byte fallback).
fn pieces(json: &[u8]) -> TractResult<Vec<Vec<u8>>> {
    let tokenizer: serde_json::Value = serde_json::from_slice(json)?;
    let vocab = tokenizer["model"]["vocab"]
        .as_object()
        .ok_or_else(|| format_err!("tokenizer.json has no BPE vocabulary"))?;
    ensure!(vocab.len() <= 65536, "Unexpected vocabulary size");
    let mut pieces = vec![vec![]; vocab.len()];
    for (piece, id) in vocab {
        let id = id.as_u64().ok_or_else(|| format_err!("Invalid token ID"))? as usize;
        ensure!(id < pieces.len(), "Token ID outside the vocabulary");
        pieces[id] = if piece.starts_with('<') && piece.ends_with('>') {
            match piece
                .strip_prefix("<0x")
                .and_then(|h| h.strip_suffix('>'))
                .and_then(|h| u8::from_str_radix(h, 16).ok())
            {
                Some(byte) => vec![byte],
                None => vec![], // <unk>, <s>, </s>
            }
        } else {
            piece.replace('▁', " ").into_bytes()
        };
    }
    Ok(pieces)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pieces_map_spaces_bytes_and_specials() {
        let json = "{\"model\":{\"vocab\":{\"<unk>\":0,\"<s>\":1,\"</s>\":2,\"<0xD8>\":3,\"<0xA7>\":4,\"\u{2581}\u{0645}\u{0631}\":5,\"\u{062d}\u{0628}\u{0627}\":6}}}".as_bytes();
        let table = pieces(json).unwrap();
        assert!(table[1].is_empty() && table[2].is_empty());
        assert_eq!(table[3], vec![0xD8]);
        // "مرحبا" from a spaced piece, plus a byte-fallback "ا" (D8 A7).
        assert_eq!(decode(&table, &[5, 6, 3, 4]), "مرحباا");
        assert_eq!(
            decode(&table, &[3]),
            "",
            "an incomplete byte sequence is dropped"
        );
    }
}
