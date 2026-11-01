#!/usr/bin/env python3
"""Prepare an experimental English BPE keyword file, without installing a model.

SentencePiece is a development-only dependency. Inference in jarvis-kws-probe
reads the resulting tokens directly and never loads Python or this tokenizer.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def encode(model_dir, phrases):
    import sentencepiece
    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(model_dir / "bpe.model"))
    vocabulary = {line.split()[0] for line in (model_dir / "tokens.txt").read_text().splitlines() if line.strip()}
    rows, aliases = [], set()
    if not 1 <= len(phrases) <= 16:
        raise ValueError("Supply 1–16 phrases")
    for phrase in phrases:
        phrase = " ".join(phrase.upper().split())
        if not phrase or len(phrase) > 64 or not all(c.isascii() and (c.isalpha() or c in " '-") for c in phrase):
            raise ValueError("Use an English wake phrase of at most 64 characters")
        alias = "_".join(phrase.replace("'", "").replace("-", " ").split())
        if not alias or alias in aliases:
            raise ValueError("Phrases must have unique labels")
        aliases.add(alias)
        ids = tokenizer.encode(phrase, out_type=int)
        pieces = tokenizer.encode(phrase, out_type=str)
        if tokenizer.unk_id() in ids or not 1 <= len(pieces) <= 32 or any(p not in vocabulary or p in {"<unk>", "<blk>"} for p in pieces):
            raise ValueError("Phrase is not representable by this model's token vocabulary: " + phrase)
        rows.append({"phrase": phrase, "alias": alias, "tokens": pieces})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("phrase", nargs="+")
    args = parser.parse_args()
    try:
        rows = encode(args.model_dir, args.phrase)
    except (ImportError, ValueError, OSError) as error:
        parser.error(str(error))
    args.output.write_text("".join(" ".join(r["tokens"]) + " @" + r["alias"] + "\n" for r in rows))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
