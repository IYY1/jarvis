# Pinned C ABI

Unmodified `c-api.h` from the installed official `sherpa_onnx==1.13.8` wheel,
upstream <https://github.com/k2-fsa/sherpa-onnx>, Apache-2.0 (`LICENSE`).
Copyright (c) 2023 Xiaomi Corporation. SHA-256:
`2a1b95084be8fd1deb3228fcad2fd3f7f0258b64582f7402281ec174c7b7f4ce`.

The probe requires that exact library version before constructing a config.
The C shim uses this header to avoid copying nested ABI layouts into Rust.
No sherpa-onnx library, ONNX Runtime library, model or tokenizer is bundled here.
