//! Arabic speech recognition with Moonshine Streaming Tiny (MIT,
//! moonshine-ai/moonshine-streaming-tiny-ar) on tract kernels, without a
//! foreign inference runtime.
//!
//! The model is three ONNX graphs exported by scripts/export_moonshine_arabic.py:
//! the encoder (16 kHz samples to hidden states with the trained sliding-window
//! attention), cross (each decoder layer's cross-attention keys and values,
//! computed once per utterance) and decoder (step-by-step KV-cached causal
//! attention with shared tied token embeddings). Decoding is greedy, capped at
//! 6.5 tokens per second of audio as the model card advises against loops.
mod model;

use std::{path::Path, sync::Arc};
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

const START: usize = 1;
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

struct Pieces {
    ranges: Box<[[u32; 2]]>,
    bytes: Box<[u8]>,
}

impl Pieces {
    fn get(&self, id: usize) -> Option<&[u8]> {
        let &[start, end] = self.ranges.get(id)?;
        self.bytes.get(start as usize..end as usize)
    }
}

pub struct Recognizer {
    encoder: Plan,
    cross: Plan,
    decoder: Plan,
    embedding: Box<[f32]>,
    pieces: Pieces,
}

fn trim() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    unsafe {
        libc::malloc_trim(0);
    }
}

fn plan(
    path: &Path,
    max_bytes: u64,
    decoder: bool,
    facts: impl FnOnce(&SymbolScope) -> Vec<InferenceFact>,
) -> TractResult<(Plan, Option<model::Span>)> {
    let canonical = path.canonicalize()?;
    let dir = canonical
        .parent()
        .and_then(Path::to_str)
        .ok_or_else(|| format_err!("Expected UTF-8 model directory"))?;
    let (proto, span) = model::load(&canonical, max_bytes, decoder)?;
    let parsed = tract_onnx::onnx().parse(&proto, Some(dir))?;
    ensure!(
        parsed.unresolved_inputs.is_empty(),
        "Unresolved ONNX inputs"
    );
    let mut typed = parsed.model;
    drop(proto);
    for (i, fact) in facts(&typed.symbols).into_iter().enumerate() {
        typed.set_input_fact(i, fact)?;
    }
    let runnable = typed.into_optimized()?.into_runnable()?;
    trim();
    Ok((runnable, span))
}

impl Recognizer {
    /// `directory` holds encoder.onnx, cross.onnx, decoder.onnx and tokenizer.json.
    pub fn load(directory: impl AsRef<Path>) -> TractResult<Self> {
        let dir = directory.as_ref();
        let (encoder, _) = plan(&dir.join("encoder.onnx"), 64 * 1024 * 1024, false, |s| {
            vec![f32::fact([1.to_dim(), s.sym("f").to_dim(), FRAME.to_dim()]).into()]
        })?;
        let (cross, _) = plan(&dir.join("cross.onnx"), 32 * 1024 * 1024, false, |s| {
            vec![f32::fact([1.to_dim(), s.sym("t").to_dim(), model::HIDDEN.to_dim()]).into()]
        })?;
        let decoder_path = dir.join("decoder.onnx");
        let (decoder, span) = plan(&decoder_path, 128 * 1024 * 1024, true, |s| {
            let (t, p) = (s.sym("t"), s.sym("p"));
            let rope = || -> InferenceFact { f32::fact([1, ROTARY]).into() };
            let cross_k = || -> InferenceFact {
                f32::fact([1.to_dim(), 8.to_dim(), 40.to_dim(), t.to_dim()]).into()
            };
            let cross_v = || -> InferenceFact {
                f32::fact([1.to_dim(), 8.to_dim(), t.to_dim(), 40.to_dim()]).into()
            };
            let self_kv = || -> InferenceFact {
                f32::fact([1.to_dim(), 8.to_dim(), p.to_dim(), 40.to_dim()]).into()
            };
            let mut inputs = vec![f32::fact([1, 1, model::HIDDEN]).into(), rope(), rope()];
            for _ in 0..model::LAYERS {
                inputs.push(cross_k());
                inputs.push(cross_v());
            }
            for _ in 0..model::LAYERS * 2 {
                inputs.push(self_kv());
            }
            inputs
        })?;
        let embedding = model::read_embedding(
            &decoder_path,
            span.ok_or_else(|| format_err!("Missing Moonshine embedding"))?,
        )?;
        let pieces = pieces(&std::fs::read(dir.join("tokenizer.json"))?)?;
        trim();
        Ok(Self {
            encoder,
            cross,
            decoder,
            embedding,
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
        let padded_len = samples.len().div_ceil(FRAME) * FRAME;
        let input = if samples.len() == padded_len {
            Tensor::from_shape(&[1, padded_len / FRAME, FRAME], samples)?
        } else {
            let mut audio = Vec::with_capacity(padded_len);
            audio.extend_from_slice(samples);
            audio.resize(padded_len, 0.0);
            Tensor::from_shape(&[1, padded_len / FRAME, FRAME], &audio)?
        };
        let memory = self.encoder.run(tvec!(input.into_tvalue()))?.remove(0);
        let mut cross = self.cross.run(tvec!(memory))?;
        let values = cross.pop().unwrap().into_tensor();
        let keys = cross.pop().unwrap().into_tensor();
        let cross_kv = split_cross_kv(&keys, &values)?;
        drop(keys);
        drop(values);

        let empty_kv = Tensor::zero::<f32>(&[1, 8, 0, 40])?.into_tvalue();
        let mut self_kv = vec![empty_kv; model::LAYERS * 2];
        let limit = (samples.len() as f64 * 6.5 / 16000.0) as usize + 2;
        let mut tokens = Vec::with_capacity(limit + 1);
        tokens.push(START as i64);
        let mut current = START;
        for step in 0..limit {
            let row = &self.embedding[current * model::HIDDEN..(current + 1) * model::HIDDEN];
            let embedded = Tensor::from_shape(&[1, 1, model::HIDDEN], row)?.into_tvalue();
            let (cos, sin) = rope_at(step);
            let cos = Tensor::from_shape(&[1, ROTARY], &cos)?.into_tvalue();
            let sin = Tensor::from_shape(&[1, ROTARY], &sin)?.into_tvalue();
            let mut inputs = tvec!(embedded, cos, sin);
            inputs.extend(cross_kv.iter().cloned());
            inputs.extend(self_kv.drain(..));
            let mut outputs = self.decoder.run(inputs)?;
            let hidden = outputs.remove(0);
            self_kv.extend(outputs);
            let hidden = hidden.try_as_plain_ram()?.as_slice::<f32>()?;
            let next = project_argmax(&self.embedding, hidden)?;
            if next == END {
                break;
            }
            tokens.push(next as i64);
            current = next;
        }
        Ok(decode(&self.pieces, &tokens[1..]))
    }
}

/// Split `[6, 1, 8, T, 40]` cross-attention keys and values once per utterance,
/// pre-transposing each layer's key tensor to `[1, 8, 40, T]`.
fn split_cross_kv(keys: &Tensor, values: &Tensor) -> TractResult<Vec<TValue>> {
    let shape = keys.shape();
    ensure!(
        shape.len() == 5
            && shape[0] == model::LAYERS
            && shape[1] == 1
            && shape[2] == 8
            && shape[4] == 40
            && values.shape() == shape,
        "Unexpected Moonshine cross-attention cache shape"
    );
    let t = shape[3];
    let head_stride = t * 40;
    let layer_stride = 8 * head_stride;
    let k_slice = keys.try_as_plain_ram()?.as_slice::<f32>()?;
    let v_slice = values.try_as_plain_ram()?.as_slice::<f32>()?;
    let mut out = Vec::with_capacity(model::LAYERS * 2);
    let mut k_transposed = vec![0.0_f32; layer_stride];
    for layer in 0..model::LAYERS {
        let k_layer = &k_slice[layer * layer_stride..(layer + 1) * layer_stride];
        for head in 0..8 {
            let src = &k_layer[head * head_stride..(head + 1) * head_stride];
            let dst = &mut k_transposed[head * head_stride..(head + 1) * head_stride];
            for frame in 0..t {
                let row = &src[frame * 40..(frame + 1) * 40];
                for dim in 0..40 {
                    dst[dim * t + frame] = row[dim];
                }
            }
        }
        out.push(Tensor::from_shape(&[1, 8, 40, t], &k_transposed)?.into_tvalue());
        let v_layer = &v_slice[layer * layer_stride..(layer + 1) * layer_stride];
        out.push(Tensor::from_shape(&[1, 8, t, 40], v_layer)?.into_tvalue());
    }
    Ok(out)
}

fn dot_f32(a: &[f32], b: &[f32]) -> f32 {
    let mut sums = [0.0_f32; 8];
    for (a, b) in a.as_chunks::<8>().0.iter().zip(b.as_chunks::<8>().0) {
        for j in 0..8 {
            sums[j] += a[j] * b[j];
        }
    }
    sums.into_iter().sum()
}

fn project_argmax(embedding: &[f32], hidden: &[f32]) -> TractResult<usize> {
    ensure!(
        hidden.len() == model::HIDDEN && hidden.iter().all(|x| x.is_finite()),
        "Non-finite logits"
    );
    let mut best_id = 0;
    let mut best_val = f32::NEG_INFINITY;
    for (i, row) in embedding
        .as_chunks::<{ model::HIDDEN }>()
        .0
        .iter()
        .enumerate()
    {
        let val = dot_f32(row, hidden);
        ensure!(val.is_finite(), "Non-finite logits");
        if i == 0 || val > best_val {
            best_id = i;
            best_val = val;
        }
    }
    Ok(best_id)
}

/// Rotary values for one token position, each frequency repeated for its pair:
/// [cos f0, cos f0, cos f1, cos f1, …], as the exported decoder expects.
fn rope_at(position: usize) -> ([f32; ROTARY], [f32; ROTARY]) {
    let mut cos = [0.0_f32; ROTARY];
    let mut sin = [0.0_f32; ROTARY];
    for pair in 0..ROTARY / 2 {
        let frequency = 1.0 / 10000_f32.powf((2 * pair) as f32 / ROTARY as f32);
        let angle = position as f32 * frequency;
        let (s, c) = angle.sin_cos();
        cos[pair * 2] = c;
        cos[pair * 2 + 1] = c;
        sin[pair * 2] = s;
        sin[pair * 2 + 1] = s;
    }
    (cos, sin)
}

/// SentencePiece-style pieces: "▁" is a space and "<0xNN>" a raw byte.
fn decode(pieces: &Pieces, tokens: &[i64]) -> String {
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
fn pieces(json: &[u8]) -> TractResult<Pieces> {
    let tokenizer: serde_json::Value = serde_json::from_slice(json)?;
    let vocab = tokenizer["model"]["vocab"]
        .as_object()
        .ok_or_else(|| format_err!("tokenizer.json has no BPE vocabulary"))?;
    ensure!(vocab.len() <= 65536, "Unexpected vocabulary size");
    let mut entries = vec![String::new(); vocab.len()];
    for (piece, id) in vocab {
        let id = id.as_u64().ok_or_else(|| format_err!("Invalid token ID"))? as usize;
        ensure!(id < entries.len(), "Token ID outside the vocabulary");
        entries[id] = piece.clone();
    }
    let mut ranges = Vec::with_capacity(entries.len());
    let mut bytes = Vec::with_capacity(entries.len() * 6);
    for piece in entries {
        let start = bytes.len() as u32;
        if piece.starts_with('<') && piece.ends_with('>') {
            if let Some(byte) = piece
                .strip_prefix("<0x")
                .and_then(|h| h.strip_suffix('>'))
                .and_then(|h| u8::from_str_radix(h, 16).ok())
            {
                bytes.push(byte);
            }
        } else if !piece.is_empty() {
            bytes.extend_from_slice(piece.replace('▁', " ").as_bytes());
        }
        ranges.push([start, bytes.len() as u32]);
    }
    Ok(Pieces {
        ranges: ranges.into_boxed_slice(),
        bytes: bytes.into_boxed_slice(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pieces_map_spaces_bytes_and_specials() {
        let json = "{\"model\":{\"vocab\":{\"<unk>\":0,\"<s>\":1,\"</s>\":2,\"<0xD8>\":3,\"<0xA7>\":4,\"\u{2581}\u{0645}\u{0631}\":5,\"\u{062d}\u{0628}\u{0627}\":6}}}".as_bytes();
        let table = pieces(json).unwrap();
        assert!(table.get(1).unwrap().is_empty() && table.get(2).unwrap().is_empty());
        assert_eq!(table.get(3).unwrap(), &[0xD8]);
        // "مرحبا" from a spaced piece, plus a byte-fallback "ا" (D8 A7).
        assert_eq!(decode(&table, &[5, 6, 3, 4]), "مرحباا");
        assert_eq!(
            decode(&table, &[3]),
            "",
            "an incomplete byte sequence is dropped"
        );
    }
}
