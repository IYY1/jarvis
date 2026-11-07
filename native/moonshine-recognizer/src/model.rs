//! Streamed ONNX loading and in-memory decoder graph rewiring for Moonshine.
//!
//! Large tensor payloads stay on disk as ONNX external-data slices until tract
//! materializes each operator, avoiding multi-copy protobuf buffers. In the
//! decoder, the tied 12,288x320 token embedding and output projection share a
//! single table, cross-attention keys/values are pre-split and pre-transposed
//! once per utterance, and the six causal self-attention layers use a
//! step-by-step KV cache so each decoding step evaluates one token.
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};
use tract_onnx::pb;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

pub const VOCAB: usize = 12288;
pub const HIDDEN: usize = 320;
pub const LAYERS: usize = 6;
/// Maximum encoder frames for 30 seconds of 16 kHz audio (<= 1,500 frames).
const MAX_POSITIONS: i64 = 1536;
const POS_EMB_NAME: &str = "dec.pos_emb.weight";
const EMBEDDING_NAME: &str = "dec.embed_tokens.weight";
const PROJECTION_NAME: &str = "onnx::MatMul_1433";

struct Cursor {
    file: File,
    buf: Vec<u8>,
    pos: usize,
    filled: usize,
    offset: u64,
    limit: u64,
}

impl Cursor {
    fn new(file: File, limit: u64) -> Self {
        Self {
            file,
            buf: vec![0; 65536],
            pos: 0,
            filled: 0,
            offset: 0,
            limit,
        }
    }
    fn next_byte(&mut self) -> TractResult<Option<u8>> {
        if self.pos == self.filled {
            ensure!(self.offset <= self.limit, "ONNX file grew past size limit");
            self.filled = self.file.read(&mut self.buf)?;
            self.pos = 0;
            if self.filled == 0 {
                return Ok(None);
            }
        }
        let b = self.buf[self.pos];
        self.pos += 1;
        self.offset += 1;
        ensure!(self.offset <= self.limit, "ONNX file exceeds size limit");
        Ok(Some(b))
    }
    fn read_varint(&mut self) -> TractResult<u64> {
        let Some(first) = self.next_byte()? else {
            return Err(format_err!("Unexpected EOF in ONNX varint"));
        };
        self.finish_varint(first)
    }
    fn finish_varint(&mut self, first: u8) -> TractResult<u64> {
        let mut value = u64::from(first & 0x7f);
        if first & 0x80 == 0 {
            return Ok(value);
        }
        for shift in (7..64).step_by(7) {
            let b = self
                .next_byte()?
                .ok_or_else(|| format_err!("Unexpected EOF in ONNX varint"))?;
            value |= u64::from(b & 0x7f) << shift;
            if b & 0x80 == 0 {
                return Ok(value);
            }
        }
        Err(format_err!("Malformed ONNX varint"))
    }
    fn copy_exact(&mut self, mut n: u64, out: &mut Vec<u8>) -> TractResult<()> {
        ensure!(
            self.offset
                .checked_add(n)
                .is_some_and(|end| end <= self.limit),
            "Unexpected EOF in ONNX field"
        );
        while n > 0 {
            if self.pos == self.filled {
                self.filled = self.file.read(&mut self.buf)?;
                self.pos = 0;
                ensure!(self.filled > 0, "Unexpected EOF in ONNX field");
            }
            let avail = (self.filled - self.pos).min(n.min(usize::MAX as u64) as usize);
            out.extend_from_slice(&self.buf[self.pos..self.pos + avail]);
            self.pos += avail;
            self.offset += avail as u64;
            n -= avail as u64;
        }
        Ok(())
    }
    fn skip(&mut self, mut n: u64) -> TractResult<()> {
        ensure!(
            self.offset
                .checked_add(n)
                .is_some_and(|end| end <= self.limit),
            "Unexpected EOF in ONNX skip"
        );
        let buffered = (self.filled - self.pos) as u64;
        if n <= buffered {
            self.pos += n as usize;
            self.offset += n;
            return Ok(());
        }
        n -= buffered;
        self.offset += buffered;
        self.pos = 0;
        self.filled = 0;
        self.file.seek(SeekFrom::Current(i64::try_from(n)?))?;
        self.offset += n;
        Ok(())
    }
    fn skip_value(&mut self, wire: u64) -> TractResult<()> {
        match wire {
            0 => {
                self.read_varint()?;
                Ok(())
            }
            1 => self.skip(8),
            2 => {
                let len = self.read_varint()?;
                self.skip(len)
            }
            5 => self.skip(4),
            _ => Err(format_err!("Unsupported protobuf wire type {wire}")),
        }
    }
    fn copy_value(&mut self, wire: u64, out: &mut Vec<u8>) -> TractResult<()> {
        match wire {
            0 => {
                let v = self.read_varint()?;
                push_varint(v, out);
                Ok(())
            }
            1 => self.copy_exact(8, out),
            2 => {
                let len = self.read_varint()?;
                push_varint(len, out);
                self.copy_exact(len, out)
            }
            5 => self.copy_exact(4, out),
            _ => Err(format_err!("Unsupported protobuf wire type {wire}")),
        }
    }
}

fn push_varint(mut v: u64, out: &mut Vec<u8>) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

#[derive(Clone, Copy)]
pub struct Span {
    pub offset: u64,
    pub length: u64,
}

pub fn load(path: &Path, max_bytes: u64, decoder: bool) -> TractResult<(pb::ModelProto, Option<Span>)> {
    let location = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| format_err!("Expected UTF-8 ONNX filename"))?
        .to_owned();
    let file = File::open(path)?;
    let len = file.metadata()?.len();
    ensure!(len <= max_bytes, "ONNX model exceeds size limit");
    let mut cursor = Cursor::new(file, len);
    let mut stripped = Vec::with_capacity(65536);
    let mut spans = Vec::new();
    while let Some(first) = cursor.next_byte()? {
        let tag = cursor.finish_varint(first)?;
        if tag == ((7 << 3) | 2) {
            let graph_len = cursor.read_varint()?;
            let graph_end = cursor
                .offset
                .checked_add(graph_len)
                .ok_or_else(|| format_err!("Overflow in ONNX graph length"))?;
            ensure!(graph_end <= len, "ONNX graph exceeds file length");
            let mut graph_bytes = Vec::with_capacity(65536);
            while cursor.offset < graph_end {
                let gtag = cursor.read_varint()?;
                if gtag == ((5 << 3) | 2) {
                    let tlen = cursor.read_varint()?;
                    let tend = cursor
                        .offset
                        .checked_add(tlen)
                        .ok_or_else(|| format_err!("Overflow in ONNX tensor length"))?;
                    ensure!(tend <= graph_end, "ONNX initializer exceeds graph");
                    let mut tbytes = Vec::new();
                    let mut raw = None;
                    while cursor.offset < tend {
                        let ttag = cursor.read_varint()?;
                        if ttag == ((9 << 3) | 2) {
                            let rlen = cursor.read_varint()?;
                            raw = Some(Span {
                                offset: cursor.offset,
                                length: rlen,
                            });
                            cursor.skip(rlen)?;
                        } else {
                            push_varint(ttag, &mut tbytes);
                            cursor.copy_value(ttag & 7, &mut tbytes)?;
                        }
                    }
                    ensure!(cursor.offset == tend, "Misaligned ONNX initializer");
                    push_varint(gtag, &mut graph_bytes);
                    push_varint(tbytes.len() as u64, &mut graph_bytes);
                    graph_bytes.extend_from_slice(&tbytes);
                    spans.push(raw);
                } else {
                    push_varint(gtag, &mut graph_bytes);
                    cursor.copy_value(gtag & 7, &mut graph_bytes)?;
                }
            }
            ensure!(cursor.offset == graph_end, "Misaligned ONNX graph");
            push_varint(tag, &mut stripped);
            push_varint(graph_bytes.len() as u64, &mut stripped);
            stripped.extend_from_slice(&graph_bytes);
        } else if (tag >> 3) == 14 && (tag & 7) == 2 {
            cursor.skip_value(2)?;
        } else {
            push_varint(tag, &mut stripped);
            cursor.copy_value(tag & 7, &mut stripped)?;
        }
    }
    let mut proto = tract_onnx::onnx().proto_model_for_read(&mut stripped.as_slice())?;
    drop(stripped);
    let graph = proto
        .graph
        .as_mut()
        .ok_or_else(|| format_err!("Missing ONNX graph"))?;
    ensure!(
        graph.initializer.len() == spans.len(),
        "ONNX initializer count mismatch"
    );
    let mut embedded_span = None;
    let mut kept = Vec::with_capacity(graph.initializer.len());
    for (mut tensor, span) in std::mem::take(&mut graph.initializer)
        .into_iter()
        .zip(spans)
    {
        if decoder && tensor.name == EMBEDDING_NAME {
            let span = span.ok_or_else(|| format_err!("Missing raw embedding span"))?;
            ensure!(
                tensor.data_type == 1
                    && tensor.dims == [VOCAB as i64, HIDDEN as i64]
                    && span.length == (VOCAB * HIDDEN * 4) as u64,
                "Unexpected Moonshine token embedding shape"
            );
            embedded_span = Some(span);
            continue;
        }
        if decoder && tensor.name == PROJECTION_NAME {
            let span = span.ok_or_else(|| format_err!("Missing raw projection span"))?;
            ensure!(
                tensor.data_type == 1
                    && tensor.dims == [HIDDEN as i64, VOCAB as i64]
                    && span.length == (VOCAB * HIDDEN * 4) as u64,
                "Unexpected Moonshine output projection shape"
            );
            continue;
        }
        if let Some(mut span) = span {
            if !decoder
                && tensor.name == POS_EMB_NAME
                && tensor.data_type == 1
                && tensor.dims == [4096, HIDDEN as i64]
            {
                tensor.dims[0] = MAX_POSITIONS;
                span.length = (MAX_POSITIONS as u64) * (HIDDEN as u64) * 4;
            }
            let kv = |key: &str, value: String| pb::StringStringEntryProto {
                key: key.into(),
                value,
            };
            tensor.data_location = Some(1);
            tensor.external_data = vec![
                kv("location", location.clone()),
                kv("offset", span.offset.to_string()),
                kv("length", span.length.to_string()),
            ];
        }
        kept.push(tensor);
    }
    graph.initializer = kept;
    if decoder {
        ensure!(embedded_span.is_some(), "Missing {EMBEDDING_NAME}");
        rewire_decoder(graph)?;
    } else {
        graph.value_info.clear();
    }
    Ok((proto, embedded_span))
}

pub fn read_embedding(path: &Path, span: Span) -> TractResult<Box<[f32]>> {
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(span.offset))?;
    let count = VOCAB * HIDDEN;
    let mut table = Vec::with_capacity(count);
    let mut buf = [0_u8; 16384];
    let mut remaining = count;
    while remaining > 0 {
        let n = remaining.min(buf.len() / 4);
        file.read_exact(&mut buf[..n * 4])?;
        for raw in buf[..n * 4].as_chunks::<4>().0 {
            let v = f32::from_le_bytes(*raw);
            ensure!(v.is_finite(), "Non-finite weight in Moonshine embedding");
            table.push(v);
        }
        remaining -= n;
    }
    Ok(table.into_boxed_slice())
}

fn value(name: &str) -> pb::ValueInfoProto {
    pb::ValueInfoProto {
        name: name.into(),
        r#type: Some(pb::TypeProto {
            value: Some(pb::type_proto::Value::TensorType(pb::type_proto::Tensor {
                elem_type: 1,
                shape: None,
            })),
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn rewire_decoder(graph: &mut pb::GraphProto) -> TractResult<()> {
    ensure!(
        graph
            .input
            .iter()
            .map(|v| v.name.as_str())
            .eq(["tokens", "keys", "values", "cos", "sin", "causal"])
            && graph.output.iter().map(|v| v.name.as_str()).eq(["logits"]),
        "Unexpected Moonshine decoder interface"
    );
    let concat = |name: String, past: String, cur: String, out: String| pb::NodeProto {
        name,
        op_type: "Concat".into(),
        input: vec![past, cur],
        output: vec![out],
        attribute: vec![pb::AttributeProto {
            name: "axis".into(),
            r#type: 2,
            i: 2,
            ..Default::default()
        }],
        ..Default::default()
    };
    let mut wired = [0_u8; LAYERS];
    let mut nodes = Vec::with_capacity(graph.node.len() + LAYERS * 2);
    for mut node in std::mem::take(&mut graph.node) {
        let drop_cross_gather = (0..LAYERS).any(|layer| {
            let k_idx = 3 * layer + 1;
            let v_idx = 3 * layer + 2;
            node.name == format!("/Gather_{k_idx}")
                || node.name == format!("/Transpose_{k_idx}")
                || node.name == format!("/Gather_{v_idx}")
        });
        if drop_cross_gather
            || matches!(
                node.name.as_str(),
                "/embed_tokens/Gather" | "/head/MatMul" | "/Gather_18"
            )
            || (node.name.starts_with("/self_attn") && node.name.ends_with("/Add_2"))
        {
            continue;
        }
        for input in &mut node.input {
            if input == "/embed_tokens/Gather_output_0" {
                *input = "embedded".into();
            }
            for layer in 0..LAYERS {
                let k_idx = 3 * layer + 1;
                let v_idx = 3 * layer + 2;
                if *input == format!("/Transpose_{k_idx}_output_0") {
                    *input = format!("cross_k_{layer}");
                    wired[layer] |= 32;
                } else if *input == format!("/Gather_{v_idx}_output_0") {
                    *input = format!("cross_v_{layer}");
                    wired[layer] |= 64;
                }
            }
        }
        let mut after = None;
        for layer in 0..LAYERS {
            let suf = if layer == 0 {
                String::new()
            } else {
                format!("_{layer}")
            };
            if node.name == format!("/self_attn{suf}/Concat_4") {
                after = Some(concat(
                    format!("/self_attn{suf}/kv_concat_k"),
                    format!("past_k_{layer}"),
                    format!("/self_attn{suf}/Concat_4_output_0"),
                    format!("present_k_{layer}"),
                ));
                wired[layer] |= 1;
            } else if node.name == format!("/self_attn{suf}/Transpose_2") {
                after = Some(concat(
                    format!("/self_attn{suf}/kv_concat_v"),
                    format!("past_v_{layer}"),
                    format!("/self_attn{suf}/Transpose_2_output_0"),
                    format!("present_v_{layer}"),
                ));
                wired[layer] |= 2;
            } else if node.name == format!("/self_attn{suf}/Transpose_3") {
                node.input[0] = format!("present_k_{layer}");
                wired[layer] |= 4;
            } else if node.name == format!("/self_attn{suf}/MatMul_3") {
                node.input[1] = format!("present_v_{layer}");
                wired[layer] |= 8;
            } else if node.name == format!("/self_attn{suf}/Softmax") {
                node.input[0] = format!("/self_attn{suf}/Mul_4_output_0");
                wired[layer] |= 16;
            }
        }
        nodes.push(node);
        if let Some(extra) = after {
            nodes.push(extra);
        }
    }
    ensure!(
        wired.iter().all(|&mask| mask == 0b111_1111),
        "Unexpected attention wiring in Moonshine decoder"
    );
    graph.node = nodes;
    let mut inputs = vec![value("embedded"), value("cos"), value("sin")];
    for layer in 0..LAYERS {
        inputs.push(value(&format!("cross_k_{layer}")));
        inputs.push(value(&format!("cross_v_{layer}")));
    }
    let mut outputs = vec![value("/norm/LayerNormalization_output_0")];
    for layer in 0..LAYERS {
        inputs.push(value(&format!("past_k_{layer}")));
        inputs.push(value(&format!("past_v_{layer}")));
        outputs.push(value(&format!("present_k_{layer}")));
        outputs.push(value(&format!("present_v_{layer}")));
    }
    graph.input = inputs;
    graph.output = outputs;
    graph.value_info.clear();
    Ok(())
}
