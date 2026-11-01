#!/usr/bin/env python3
"""Measure the isolated Rust KWS probe with explicit local public fixtures.

No microphone, desktop actions, model installation or production setting change.
The probe uses native sherpa-onnx/ONNX Runtime; these count in its RSS/PSS. This
does not measure a complete on-demand command recognizer or Windows deployment.
"""
from __future__ import annotations
import argparse
import array
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def silence(seconds):
    return bytes(round(seconds * 16000) * 2)


def reverse(pcm):
    samples = array.array("h", pcm)
    samples.reverse()
    return samples.tobytes()


def cases(fixture, model, folder):
    pcm = fixture.read_bytes()
    if not 6400 <= len(pcm) <= 320000 or len(pcm) % 2:
        raise ValueError("Supply 0.2–10 seconds of s16le 16 kHz mono PCM")
    # These cuts refer specifically to the pinned public goforward fixture.
    if hashlib.sha256(pcm).hexdigest() != "f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71":
        raise ValueError("These labelled cuts require the documented goforward.raw fixture")
    lead, tail = silence(.6), silence(1.5)
    rng = random.Random(42)
    noisy = array.array("h", (max(-32768, min(32767, round(s + rng.uniform(-.02, .02) * 32768))) for s in array.array("h", pcm))).tobytes()
    low = array.array("h", (round(s * .1) for s in array.array("h", pcm))).tobytes()
    rows = []
    def add(name, group, audio, expected, paced=False):
        path = folder / (name + ".raw")
        path.write_bytes(audio)
        rows.append({"name": name, "group": group, "path": str(path.resolve()), "sha256": digest(path), "seconds": len(audio)/32000,
                     "expected": expected, "paced": paced})
    add("natural", "surrogate", lead + pcm + tail, ["GO_FORWARD"], True)
    add("wake-only", "surrogate", lead + pcm[:38400] + tail, ["GO_FORWARD"])
    add("quiet-recovery", "surrogate", lead + (reverse(pcm)*3 + silence(1.5) + pcm + silence(4))*3, ["GO_FORWARD"]*3, True)
    add("pause-inside-name", "surrogate", lead + pcm[:21120] + silence(.3) + pcm[21120:] + tail, ["GO_FORWARD"], True)
    add("low-volume", "surrogate", lead + low + tail, ["GO_FORWARD"])
    add("uniform-noise", "surrogate", lead + noisy + tail, ["GO_FORWARD"])
    add("command-without-wake", "surrogate", lead + (pcm[38400:] + tail)*3, [], True)
    add("reversed-speech", "surrogate", lead + (reverse(pcm) + tail)*3, [], True)
    add("silence", "surrogate", silence(10), [], True)
    for n, keyword in [(0, "LIGHT_UP"), (1, "LOVELY_CHILD")]:
        with wave.open(str(model / "test_wavs" / f"{n}.wav")) as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Expected public model fixture s16le/16k/mono")
            speech = wav.readframes(wav.getnframes())
        add(f"published-{n}", "published", lead + speech + tail, [keyword])
        add(f"unrelated-{n}", "surrogate", lead + speech + tail, [])
    # No positive recording of Jarvis: these are negative controls only.
    for row in list(rows):
        if row["group"] in {"surrogate", "published"} and row["name"] in {"natural", "published-0", "published-1"}:
            rows.append(dict(row, name="names-negative-" + row["name"], group="names", expected=[], paced=False))
    return rows


def reference(config, rows):
    import sherpa_onnx
    recognizer = sherpa_onnx.KeywordSpotter(**{k:config[k] for k in ("tokens", "encoder", "decoder", "joiner")},
        keywords_file=config["keywords"], num_threads=1, keywords_score=config["score"], keywords_threshold=config["threshold"])
    result = []
    for row in rows:
        stream = recognizer.create_stream()
        samples = array.array("h", Path(row["path"]).read_bytes())
        hits = []
        def drain(seconds):
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
                keyword = recognizer.get_result(stream)
                if keyword:
                    hits.append({"keyword": keyword, "audio_seconds": seconds})
                    recognizer.reset_stream(stream)
        for start in range(0, len(samples), 480):
            stream.accept_waveform(16000, [s/32768 for s in samples[start:start+480]])
            drain(min(start+480,len(samples))/16000)
        stream.input_finished()
        drain(len(samples)/16000)
        result.append({"name":row["name"], "hits":hits})
    return result


def probe(binary, config_path, rows, paced):
    process = subprocess.run([str(binary.resolve()), str(config_path.resolve())],
        input="".join(json.dumps({"path":r["path"],"paced":paced})+"\n" for r in rows), text=True,
        capture_output=True, check=True, timeout=60+sum(r["seconds"] for r in rows)*(1 if paced else .2))
    events = [json.loads(line) for line in process.stdout.splitlines()]
    if not events or events[0].get("event") != "ready":
        raise RuntimeError("Probe did not become ready")
    measured, pending = [], []
    for event in events[1:]:
        if event["event"] == "keyword":
            pending.append(event)
        elif event["event"] == "complete":
            row = rows[len(measured)]
            if event["path"] != row["path"] or event["detections"] != len(pending):
                raise RuntimeError("Incomplete or reordered probe response")
            actual = [e["result"]["keyword"] for e in pending]
            measured.append({"name":row["name"], "expected":row["expected"], "actual":actual,
                "passed":actual==row["expected"],"hits":pending,"measurement":event})
            pending=[]
        else:
            raise RuntimeError("Unexpected probe event")
    if pending or len(measured)!=len(rows):
        raise RuntimeError("Incomplete probe run")
    return {"ready":events[0],"cases":measured,"all_cases_pass":all(r["passed"] for r in measured)}


def vosk(binary, rows):
    from benchmark_recognition_acceptance import run_case
    from murmur.commands import Parser
    from murmur.config import validate
    from murmur.native_worker import prepare
    config = validate({"recognition":{"accurate":False,"noise_suppression":False},"activation":{"wake_phrase":"go forward","command_timeout":3}})
    grammar = Parser({"ten meters":"start recording"}).grammar(["firefox","steam","stremio"])
    config["privacy"]["onboarded"]=True
    with patch.dict(os.environ,MURMUR_VOICE_WORKER="rust"):
        _,payload,env=prepare(config,"wake_word",grammar)
    result=[]
    for row in rows:
        print("Vosk baseline: "+row["name"],flush=True)
        measured=run_case(binary,payload,env,Path(row["path"]).read_bytes())
        count=sum(e.get("event")=="wake" for e in measured["events"])
        result.append({"name":row["name"],"expected_wakes":len(row["expected"]),"actual_wakes":count,
            "wake_count_matches":count==len(row["expected"]) and measured["error"] is None, **measured})
    return {"scope":"normal Rust worker, three resident Vosk decoders, raw PCM and energy gate; accurate/noise_suppression disabled to isolate wake path; no actions",
            "binary_sha256":digest(binary),"cases":result}


def summarize(runs, baseline=None):
    settings = {}
    for run in runs:
        key = f"{run['group']}/{run['threshold']}/{'paced' if run['paced'] else 'unpaced'}"
        row = settings.setdefault(key, {"workers":0,"cases":0,"passed_cases":0,"expected_detections":0,"correct_detections":0,"missed_detections":0,"extra_detections":0})
        row["workers"] += 1
        for case in run["cases"]:
            expected, actual = Counter(case["expected"]), Counter(case["actual"])
            row["cases"] += 1
            row["passed_cases"] += case["passed"]
            row["expected_detections"] += expected.total()
            row["correct_detections"] += (actual & expected).total()
            row["missed_detections"] += (expected - actual).total()
            row["extra_detections"] += (actual - expected).total()
    paced=[r for r in runs if r["paced"]]
    primary=[r for r in runs if r["group"]=="surrogate" and r["threshold"]==.25]
    return {"reference_identical":all(c.get("reference_identical",True) for r in runs for c in r["cases"]),
        "reference_cases":sum("reference_identical" in c for r in runs for c in r["cases"]),
        "focused_cases_pass":all(r["all_cases_pass"] for r in runs),
        "primary_cases_pass":bool(primary) and all(r["all_cases_pass"] for r in primary),
        "vosk_baseline_cases_pass":all(c["wake_count_matches"] for c in baseline["cases"]) if baseline else None,
        "settings":settings,
        "paced_ready_rss_mib":[r["ready"]["memory"]["rss_mib"] for r in paced],
        "paced_peak_rss_mib":[max(c["measurement"]["memory"]["peak_rss_mib"] for c in r["cases"]) for r in paced],
        "paced_final_pss_mib":[r["cases"][-1]["measurement"]["memory"]["pss_mib"] for r in paced],
        "silence_cpu_percent_one_core":[100*c["measurement"]["cpu_seconds"]/c["measurement"]["audio_seconds"] for r in paced for c in r["cases"] if c["name"]=="silence"],
        "paced_natural_detection_audio_seconds":[c["hits"][0]["audio_seconds"] for r in paced for c in r["cases"] if c["name"]=="natural" and c["hits"]]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary",type=Path,default=ROOT/"native/kws-probe/target/release/jarvis-kws-probe")
    p.add_argument("--fixture",type=Path,required=True)
    p.add_argument("--model-dir",type=Path,required=True)
    p.add_argument("--library",type=Path,required=True)
    p.add_argument("--keywords-dir",type=Path,required=True)
    p.add_argument("--work-dir",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--voice-binary",type=Path)
    p.add_argument("--repeat",type=int,default=3)
    p.add_argument("--reference",type=Path,help=argparse.SUPPRESS)
    args=p.parse_args()
    if args.reference:
        bundle=json.loads(args.reference.read_text());print(json.dumps(reference(bundle["config"],bundle["rows"])));return 0
    if sys.platform!="linux" or not 1<=args.repeat<=3:
        p.error("This measurement supports Linux and 1–3 fresh repeats")
    args.work_dir.mkdir(parents=True,exist_ok=True)
    rows=cases(args.fixture,args.model_dir,args.work_dir)
    config={"library":str(args.library.resolve()),"tokens":str((args.model_dir/"tokens.txt").resolve()),"score":1.0,"threshold":.25}
    for key in ("encoder","decoder","joiner"):
        config[key]=str(next(args.model_dir.glob(key+"*.int8.onnx")).resolve())
    result={"schema_version":1,"recorded_at":datetime.now(timezone.utc).isoformat(),"platform":platform.platform(),"scope":__doc__,
        "binary_sha256":digest(args.binary),"inputs":rows,"model_and_library":{k:{"path":v,"sha256":digest(v)} for k,v in config.items() if isinstance(v,str)},
        "configuration":{"score":1.0,"thresholds":[.1,.25,.5,.75],"threads":1},"runs":[],"complete":False,
        "production_settings_changed":False,"full_acoustic_acceptance":False,"windows_acceptance":False,"whole_app_budget_acceptance":False}
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+"\n")
    for group in ("surrogate","published","names"):
        selected=[r for r in rows if r["group"]==group]
        config["keywords"]=str((args.keywords_dir/(group+"-keywords.txt")).resolve())
        for threshold in ([.1,.25,.5,.75] if group=="surrogate" else [.25]):
            config["threshold"]=threshold
            config_path=args.work_dir/f"{group}-{threshold}-config.json";config_path.write_text(json.dumps(config))
            bundle=args.work_dir/"reference.json";bundle.write_text(json.dumps({"config":config,"rows":selected}))
            common=["--fixture",str(args.fixture),"--model-dir",str(args.model_dir),"--library",str(args.library),"--keywords-dir",str(args.keywords_dir),"--work-dir",str(args.work_dir),"--output",str(args.output)]
            ref=json.loads(subprocess.run([sys.executable,__file__,*common,"--reference",str(bundle)],capture_output=True,text=True,check=True,timeout=180).stdout)
            for repeat in range(args.repeat):
                print(f"KWS {group}, threshold {threshold}, repeat {repeat+1}",flush=True)
                run=probe(args.binary,config_path,selected,False)
                for measured,expected in zip(run["cases"],ref,strict=True):
                    actual=[{"keyword":e["result"]["keyword"],"audio_seconds":e["audio_seconds"]} for e in measured["hits"]]
                    measured["reference"]=expected["hits"]
                    measured["reference_identical"]=actual==expected["hits"]
                result["runs"].append({"group":group,"threshold":threshold,"repeat":repeat+1,"paced":False,**run});save()
            if group=="surrogate" and threshold==.25:
                for repeat in range(args.repeat):
                    print(f"KWS paced primary, repeat {repeat+1}",flush=True)
                    result["runs"].append({"group":group,"threshold":threshold,"repeat":repeat+1,"paced":True,
                        **probe(args.binary,config_path,[r for r in selected if r["paced"]],True)});save()
    if args.voice_binary:
        result["vosk_baseline"]=vosk(args.voice_binary,[r for r in rows if r["group"]=="surrogate" and r["paced"]]);save()
    result["summary"]=summarize(result["runs"],result.get("vosk_baseline"))
    result["complete"]=True;save()
    print(json.dumps(result["summary"],indent=2))
    return 0 if (result["summary"]["reference_identical"] and result["summary"]["focused_cases_pass"]
                 and result["summary"]["vosk_baseline_cases_pass"] is not False) else 1


if __name__=="__main__":
    raise SystemExit(main())
