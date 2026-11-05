#!/usr/bin/env python3
"""Compare local Arabic speech recognizers on the same audio; never downloads.

Each model runs in a fresh child process so load time, peak RSS and CPU are
its own. Audio and models are supplied explicitly (see docs/arabic.md); the
manifest lists WAV files (16 kHz mono s16) with reference text. Scores use the
usual Arabic normalization: diacritics and tatweel removed, alef forms unified,
ta marbuta -> ha, alef maqsura -> ya, punctuation removed, Latin lowercased.

Run with a venv that has the runtime each model needs (sherpa-onnx, vosk,
transformers+torch for the two Hugging Face checkpoints).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import sys
import time
import unicodedata
import wave

DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي",
                      **{chr(0x660 + i): str(i) for i in range(10)},
                      **{chr(0x6F0 + i): str(i) for i in range(10)}})


def normalize(text):
    text = DIACRITICS.sub("", unicodedata.normalize("NFKC", text)).translate(FOLD).lower()
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return " ".join(text.split())


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def score(pairs):
    words = chars = ref_words = ref_chars = 0
    for ref, hyp in pairs:
        r, h = normalize(ref), normalize(hyp)
        words += distance(r.split(), h.split())
        chars += distance(r.replace(" ", ""), h.replace(" ", ""))
        ref_words += len(r.split())
        ref_chars += len(r.replace(" ", ""))
    return {"wer": round(100 * words / max(ref_words, 1), 2), "cer": round(100 * chars / max(ref_chars, 1), 2)}


def samples(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        import numpy as np
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768


def recognizer(spec, threads):
    kind, root = spec["kind"], Path(spec["path"])
    if kind == "vosk":
        import vosk
        vosk.SetLogLevel(-1)
        model = vosk.Model(str(root))

        def run(audio):
            rec = vosk.KaldiRecognizer(model, 16000)
            rec.AcceptWaveform((audio * 32768).astype("<i2").tobytes())
            return json.loads(rec.FinalResult())["text"]
        return run
    if kind in ("hf-whisper", "hf-moonshine"):
        import torch
        torch.set_num_threads(threads)
        from transformers import AutoProcessor
        processor = AutoProcessor.from_pretrained(root)
        if kind == "hf-whisper":
            from transformers import WhisperForConditionalGeneration as Model
        else:
            from transformers import MoonshineStreamingForConditionalGeneration as Model
        model = Model.from_pretrained(root).eval()

        def run(audio):
            inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
            with torch.inference_mode():
                if kind == "hf-whisper":
                    out = model.generate(inputs.input_features, language="ar", task="transcribe", max_new_tokens=200)
                else:
                    limit = int(len(audio) * 6.5 / 16000) + 2
                    out = model.generate(**inputs, max_new_tokens=limit)
            return processor.batch_decode(out, skip_special_tokens=True)[0]
        return run
    import sherpa_onnx as so
    if kind == "whisper":
        name = spec["name"]
        rec = so.OfflineRecognizer.from_whisper(
            encoder=str(root / f"{name}-encoder.int8.onnx"), decoder=str(root / f"{name}-decoder.int8.onnx"),
            tokens=str(root / f"{name}-tokens.txt"), language=spec.get("language", "ar"), num_threads=threads)
    elif kind == "moonshine-v2":
        rec = so.OfflineRecognizer.from_moonshine_v2(
            encoder=str(root / "encoder_model.ort"), decoder=str(root / "decoder_model_merged.ort"),
            tokens=str(root / "tokens.txt"), num_threads=threads)
    elif kind == "qwen3":
        rec = so.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(root / "conv_frontend.onnx"), encoder=str(root / "encoder.int8.onnx"),
            decoder=str(root / "decoder.int8.onnx"), tokenizer=str(root / "tokenizer"), num_threads=threads)
    elif kind == "omnilingual":
        rec = so.OfflineRecognizer.from_omnilingual_asr_ctc(
            model=str(root / "model.int8.onnx"), tokens=str(root / "tokens.txt"), num_threads=threads)
    elif kind == "zipformer-streaming":
        online = so.OnlineRecognizer.from_transducer(
            encoder=str(next(root.glob("encoder*.int8.onnx"))), decoder=str(next(root.glob("decoder*.onnx"))),
            joiner=str(next(root.glob("joiner*.int8.onnx"))), tokens=str(root / "tokens.txt"), num_threads=threads)

        def run(audio):
            import numpy as np
            stream = online.create_stream()
            stream.accept_waveform(16000, audio)
            stream.accept_waveform(16000, np.zeros(8000, dtype="float32"))
            stream.input_finished()
            while online.is_ready(stream):
                online.decode_stream(stream)
            return online.get_result(stream)
        return run
    else:
        raise SystemExit(f"Unknown model kind {kind}")

    def run(audio):
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        return stream.result.text
    return run


def child(spec, manifest, threads):
    start = time.perf_counter()
    run = recognizer(spec, threads)
    load = time.perf_counter() - start
    run(samples(manifest[0]["path"]))   # warm-up, not scored
    rows, audio_seconds, wall, cpu0 = [], 0.0, 0.0, time.process_time()
    for item in manifest:
        audio = samples(item["path"])
        t = time.perf_counter()
        text = run(audio)
        took = time.perf_counter() - t
        wall += took
        audio_seconds += len(audio) / 16000
        rows.append({"id": item["id"], "set": item["set"], "ref": item["text"], "hyp": text, "seconds": round(took, 3)})
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(json.dumps({"load_seconds": round(load, 2), "rtf": round(wall / audio_seconds, 4),
                      "cpu_rtf": round((time.process_time() - cpu0) / audio_seconds, 4),
                      "peak_rss_mib": round(usage.ru_maxrss / 1024, 1), "rows": rows}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True, help="JSON list of {id,set,path,text}")
    parser.add_argument("--models", type=Path, required=True, help="JSON list of {label,kind,path,license,...}")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--only", nargs="*", help="model labels to run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    models = json.loads(args.models.read_text())
    if args.child:
        child(next(m for m in models if m["label"] == args.child), manifest, args.threads)
        return
    previous = json.loads(args.output.read_text()) if args.output.exists() else {"models": {}}
    for spec in models:
        if args.only and spec["label"] not in args.only:
            continue
        print(f"{spec['label']}…", file=sys.stderr, flush=True)
        proc = subprocess.run([sys.executable, "-I", __file__, "--manifest", str(args.manifest.resolve()),
                               "--models", str(args.models.resolve()), "--threads", str(args.threads),
                               "--output", str(args.output), "--child", spec["label"]],
                              capture_output=True, text=True, env={**os.environ, "OMP_NUM_THREADS": str(args.threads)})
        if proc.returncode:
            previous["models"][spec["label"]] = {"error": proc.stderr.strip()[-2000:]}
            continue
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        sets = {}
        for row in result["rows"]:
            sets.setdefault(row["set"], []).append((row["ref"], row["hyp"]))
        result["scores"] = {name: {**score(pairs), "clips": len(pairs)} for name, pairs in sets.items()}
        result["model"] = {k: v for k, v in spec.items() if k != "path"}
        result["disk_mib"] = round(sum(f.stat().st_size for f in Path(spec["path"]).rglob("*")
                                       if f.is_file() and "test_wavs" not in f.parts) / 2**20, 1)
        previous["models"][spec["label"]] = result
        print(json.dumps({spec["label"]: result["scores"], "rtf": result["rtf"], "rss": result["peak_rss_mib"]},
                         ensure_ascii=False), file=sys.stderr)
    previous.update({"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "machine": {"cpu": platform.processor() or platform.machine(), "threads": args.threads,
                                 "python": platform.python_version()}})
    args.output.write_text(json.dumps(previous, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
