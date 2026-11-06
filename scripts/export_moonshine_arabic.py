#!/usr/bin/env python3
"""Export Moonshine Streaming Tiny Arabic to the ONNX graphs Jarvis runs.

Maintainer tool; Jarvis users install the resulting archive with
`jarvis install-model --arabic`. Needs torch, transformers (5.2+), soundfile
and onnx in a separate venv, plus a local copy of the model at the pinned
revision (MIT, https://huggingface.co/moonshine-ai/moonshine-streaming-tiny-ar).

    python export_moonshine_arabic.py MODEL_DIR OUT_DIR CLIP.wav [CLIP.wav ...]

The wrappers rebuild the sliding-window attention masks from plain tensor
ops so the graphs trace, keep their inputs unmodified, and take the decoder's
rotary tables and causal mask as inputs (tract cannot infer the shapes the
traced versions produce). Every clip is decoded both ways; the export is
only valid when the wrapper matches transformers' generate() on all of them.
Writes encoder.onnx (5 ms frames -> states), cross.onnx (states -> per-layer
cross-attention keys/values) and decoder.onnx (tokens -> next-token logits).
"""
import json
import sys

import soundfile as sf
import torch
from transformers import MoonshineStreamingForConditionalGeneration, AutoProcessor

root, out = sys.argv[1], sys.argv[2]
clips = sys.argv[3:]
model = MoonshineStreamingForConditionalGeneration.from_pretrained(root, attn_implementation="eager").eval()
proc = AutoProcessor.from_pretrained(root)
enc, dec = model.model.encoder, model.model.decoder
windows = model.config.encoder_config.sliding_windows
NEG = -1e9


def asinh(self, x):
    # Same function as torch.asinh, written with ops ONNX export supports.
    y = torch.exp(self.log_k) * x
    return torch.sign(y) * torch.log(y.abs() + torch.sqrt(y * y + 1))


type(enc.embedder.comp).forward = asinh
import transformers.models.moonshine_streaming.modeling_moonshine_streaming as mm


# Rotary position embedding for tract: the caller supplies cos/sin already
# interleaved ([c0, c0, c1, c1, ...] for the 32 rotated dimensions), and the
# pair rotation (x0, x1) -> (-x1, x0) is a constant matrix product.
ROTATE = torch.zeros(32, 32)
for i in range(16):
    ROTATE[2 * i + 1, 2 * i] = -1.0
    ROTATE[2 * i, 2 * i + 1] = 1.0


def causal_mask(length):
    n = torch.arange(length)
    return torch.where(n[:, None] >= n[None, :], 0.0, NEG)[None, None]


def rope_tables(length):
    inv = 1.0 / (10000 ** (torch.arange(0, 32, 2, dtype=torch.float) / 32))
    angles = torch.arange(length, dtype=torch.float)[:, None] * inv[None]
    return angles.cos().repeat_interleave(2, -1), angles.sin().repeat_interleave(2, -1)


def apply_rotary_pos_emb(q, k, cos, sin, unsqueeze_dim=1):
    cos, sin = cos.unsqueeze(unsqueeze_dim), sin.unsqueeze(unsqueeze_dim)
    q_rot, q_pass = q[..., :32], q[..., 32:]
    k_rot, k_pass = k[..., :32], k[..., 32:]
    return (torch.cat([q_rot * cos + torch.matmul(q_rot, ROTATE) * sin, q_pass], dim=-1),
            torch.cat([k_rot * cos + torch.matmul(k_rot, ROTATE) * sin, k_pass], dim=-1))


ORIGINAL_ROPE = mm.apply_rotary_pos_emb


class Encoder(torch.nn.Module):
    def __init__(s):
        super().__init__()
        s.enc = enc

    def forward(s, frames):           # [1, F, 80]: 5 ms frames of 16 kHz samples
        e = s.enc.embedder
        h = torch.nn.functional.silu(e.linear(e.comp(e.cmvn(frames))))
        h, _ = e.conv1(h.transpose(1, 2), None)
        h, _ = e.conv2(torch.nn.functional.silu(h), None)
        h = h.transpose(1, 2)
        t = torch.arange(h.shape[1])
        dist = t[:, None] - t[None, :]
        for (left, right), layer in zip(windows, s.enc.layers):
            allowed = ((dist >= 0) & (dist < left)) | ((dist < 0) & (-dist < right))
            mask = torch.where(allowed, 0.0, NEG)[None, None]
            h = layer(h, attention_mask=mask)
        return s.enc.final_norm(h)


class Cross(torch.nn.Module):
    """Cross-attention keys and values for every decoder layer, computed once."""
    def __init__(s):
        super().__init__()
        s.dec = dec

    def forward(s, memory):           # [1, T, 320] -> k, v [layers, 1, heads, T, 40]
        m = s.dec.proj(memory + s.dec.pos_emb.weight[: memory.shape[1]][None])
        shape = (1, memory.shape[1], 8, 40)
        k = [l.encoder_attn.k_proj(m).view(shape).transpose(1, 2) for l in s.dec.layers]
        v = [l.encoder_attn.v_proj(m).view(shape).transpose(1, 2) for l in s.dec.layers]
        return torch.stack(k), torch.stack(v)


class Decoder(torch.nn.Module):
    def __init__(s):
        super().__init__()
        s.dec, s.head = dec, model.proj_out

    def forward(s, tokens, keys, values, cos, sin, causal):
        # tokens [1, L] int64; cos/sin [L, 32]; causal [1, 1, L, L] additive mask
        x = s.dec.embed_tokens(tokens)
        pe = (cos[None], sin[None])
        for i, layer in enumerate(s.dec.layers):
            h, _ = layer.self_attn(hidden_states=layer.input_layernorm(x), attention_mask=causal,
                                   position_embeddings=pe, past_key_values=None)
            x = x + h
            a = layer.encoder_attn
            q = a.q_proj(layer.post_attention_layernorm(x)).view(1, x.shape[1], 8, 40).transpose(1, 2)
            w = torch.softmax(torch.matmul(q, keys[i].transpose(2, 3)) * a.scaling, dim=-1)
            o = torch.matmul(w, values[i]).transpose(1, 2).reshape(1, x.shape[1], 320)
            x = x + a.o_proj(o)
            x = x + layer.mlp(layer.final_layernorm(x))
        return s.head(s.dec.norm(x))[:, -1]   # logits for the next token only


E, C, D = Encoder().eval(), Cross().eval(), Decoder().eval()


def greedy(audio):
    pad = (-len(audio)) % 80
    a = torch.nn.functional.pad(torch.from_numpy(audio)[None], (0, pad)).reshape(1, -1, 80)
    mem = E(a)
    keys, values = C(mem)
    toks = [1]
    for _ in range(int(len(audio) * 6.5 / 16000) + 2):
        cos, sin = rope_tables(len(toks))
        nxt = D(torch.tensor([toks]), keys, values, cos, sin, causal_mask(len(toks)))[0].argmax().item()
        if nxt == 2:
            break
        toks.append(nxt)
    return proc.batch_decode([toks], skip_special_tokens=True)[0], a, mem, toks, keys, values


agree = 0
with torch.no_grad():
    for path in clips:
        audio, _ = sf.read(path, dtype="float32")
        inputs = proc(audio, sampling_rate=16000, return_tensors="pt")
        mm.apply_rotary_pos_emb = ORIGINAL_ROPE
        ref = proc.batch_decode(model.generate(**inputs, max_new_tokens=int(len(audio) * 6.5 / 16000) + 2),
                                skip_special_tokens=True)[0]
        mm.apply_rotary_pos_emb = apply_rotary_pos_emb
        mine, a, mem, toks, keys, values = greedy(audio)
        agree += ref.strip() == mine.strip()
        print(json.dumps({"clip": path.split("/")[-1], "generate": ref, "wrapper": mine}, ensure_ascii=False))
print(f"wrapper == generate on {agree}/{len(clips)} clips")
if agree != len(clips):
    sys.exit("Export wrappers disagree with transformers; not exporting")
torch.onnx.export(E, (a,), f"{out}/encoder.onnx", input_names=["frames"], output_names=["memory"],
                  dynamic_axes={"frames": {1: "f"}, "memory": {1: "t"}}, opset_version=17, dynamo=False)
torch.onnx.export(C, (mem,), f"{out}/cross.onnx", input_names=["memory"], output_names=["keys", "values"],
                  dynamic_axes={"memory": {1: "t"}, "keys": {3: "t"}, "values": {3: "t"}}, opset_version=17, dynamo=False)
cos, sin = rope_tables(len(toks))
torch.onnx.export(D, (torch.tensor([toks]), keys.clone(), values.clone(), cos, sin, causal_mask(len(toks))),
                  f"{out}/decoder.onnx", input_names=["tokens", "keys", "values", "cos", "sin", "causal"],
                  output_names=["logits"],
                  dynamic_axes={"tokens": {1: "l"}, "keys": {3: "t"}, "values": {3: "t"}, "cos": {0: "l"},
                                "sin": {0: "l"}, "causal": {2: "l", 3: "l"}},
                  opset_version=17, dynamo=False)
print("exported")
