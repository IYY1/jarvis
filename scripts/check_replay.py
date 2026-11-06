#!/usr/bin/env python3
"""Replay a bounded local 16 kHz mono PCM fixture through capture→voice→IPC.

Uses a temporary fake pw-record executable, never a microphone. The upstream
goforward.raw sample can be supplied explicitly. No fixture is bundled.
--wake uses the beginning of the fixture as a surrogate keyword phrase and
the remainder as a one-breath command. Every desktop action is dry-run only.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.config import DEFAULTS


FAKE_CAPTURE = '''#!/usr/bin/env python3
import os,sys,time,math,struct
from pathlib import Path
data=Path(os.environ['JARVIS_FIXTURE']).read_bytes()
command_start=int(os.environ['JARVIS_COMMAND_START'])
threshold=float(os.environ['JARVIS_ENDPOINT_RMS'])
# Select the final energetic frame across the WHOLE command fixture before
# playback; an early recognition cannot accidentally redefine this endpoint.
last_energy=None
for i in range(0,len(data),960):
    if i < command_start:
        continue
    frame=data[i:i+960]
    samples=struct.unpack('<'+'h'*(len(frame)//2),frame)
    if math.sqrt(sum(x*x for x in samples)/max(1,len(samples)))/32768 > threshold:
        last_energy=i
if last_energy is None:
    raise RuntimeError('No command energy above the endpoint proxy threshold')
# Wait for the status observer so even delayed subscription sees the wake.
ready=Path(os.environ['JARVIS_PLAYBACK_READY'])
until=time.monotonic()+15
while not ready.exists():
    if time.monotonic()>until:
        raise RuntimeError('Status observer did not release fixture playback')
    # Keep the fake microphone alive while speech playback is gated.
    sys.stdout.buffer.write(bytes(960))
    sys.stdout.buffer.flush()
    time.sleep(.03)
for i in range(0,len(data),960):
    emitted=time.monotonic()
    sys.stdout.buffer.write(data[i:i+960].ljust(960,b'\\0'))
    sys.stdout.buffer.flush()
    if i==last_energy:
        Path(os.environ['JARVIS_SPEECH_END']).write_text(str(emitted+.03))
    time.sleep(.03)
time.sleep(2)
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("raw", type=Path, help="Upstream PocketSphinx goforward.raw sample")
    p.add_argument("--wake", action="store_true", help="Exercise hands-free one-breath keyword activation and command")
    p.add_argument("--endpoint-rms", type=float, default=.01,
                   help="Fixed waveform-energy endpoint proxy (default .01); not human speech ground truth")
    args = p.parse_args()
    if not 1000 < args.raw.stat().st_size <= 480000:
        p.error("Fixture must contain at most 15 seconds of raw PCM")
    if not 0 < args.endpoint_rms <= .5:
        p.error("Endpoint RMS must be greater than zero and at most .5")
    audio = args.raw.read_bytes()
    if len(audio) % 2:
        p.error("Fixture must contain complete 16-bit samples")
    with tempfile.TemporaryDirectory(prefix="jarvis-replay-") as temp:
        base = Path(temp)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["privacy"]["onboarded"] = True
        cfg["activation"]["mode"] = "wake_word" if args.wake else "push_to_talk"
        if args.wake:
            cfg["activation"]["wake_phrase"] = "go forward"
        cfg["audio"]["vad_rms"] = .001
        cfg["commands"] = {"go forward ten meters": "turn the volume down"}
        if args.wake:
            cfg["commands"]["ten meters"] = "turn the volume down"
        (base / "config.json").write_text(json.dumps(cfg))
        prefix = bytes(16000)
        (base / "input.raw").write_bytes(prefix + audio + bytes(32000))
        fake = base / "pw-record"
        fake.write_text(FAKE_CAPTURE)
        fake.chmod(0o700)
        env = dict(os.environ, JARVIS_CONFIG=str(base / "config.json"),
                   JARVIS_SOCKET=str(base / "ipc/control.sock"), JARVIS_DRY_RUN="1",
                   JARVIS_FIXTURE=str(base / "input.raw"),
                   JARVIS_COMMAND_START=str(len(prefix)), JARVIS_ENDPOINT_RMS=str(args.endpoint_rms),
                   JARVIS_PLAYBACK_READY=str(base / "playback-ready"),
                   JARVIS_SPEECH_END=str(base / "speech-end.txt"))
        env["PATH"] = str(base) + os.pathsep + env["PATH"]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        old_socket = os.environ.get("JARVIS_SOCKET")
        os.environ["JARVIS_SOCKET"] = env["JARVIS_SOCKET"]
        from jarvis.client import request
        daemon = None
        subscriber = None
        try:
            daemon = subprocess.Popen([sys.executable, "-m", "jarvis", "daemon"], env=env, stderr=subprocess.PIPE)
            until = time.monotonic() + 5
            while not Path(env["JARVIS_SOCKET"]).exists():
                if daemon.poll() is not None:
                    raise RuntimeError("Replay daemon did not start: " + daemon.stderr.read().decode()[:1000])
                if time.monotonic() > until:
                    raise RuntimeError("Replay daemon did not create its socket within five seconds")
                time.sleep(.05)
            subscriber = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            subscriber.settimeout(.25)
            subscriber.connect(env["JARVIS_SOCKET"])
            subscriber.sendall(b'{"op":"watch"}\n')
            if not args.wake:
                assert request("listen")["ok"]
            started = time.monotonic()
            wake_observed = False
            pending = b""
            while time.monotonic() - started < 12:
                try:
                    data = subscriber.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise RuntimeError("Replay status subscription closed before dispatch")
                pending += data
                if len(pending) > 1048576:
                    raise RuntimeError("Replay status exceeded the response limit")
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    event = json.loads(line)
                    if not event.get("ok"):
                        raise RuntimeError(event.get("error", "Replay status failed"))
                    (base / "playback-ready").touch(exist_ok=True)
                    status = event["status"]
                    if status["message"] == "Listening · say one command":
                        wake_observed = True
                    if status["message"] == "Dry run: volume_down":
                        if args.wake and not wake_observed:
                            raise RuntimeError("Dispatch occurred without observing wake activation")
                        observed = time.monotonic()
                        endpoint = base / "speech-end.txt"
                        if not endpoint.exists():
                            raise RuntimeError("Dispatch preceded the final energetic frame of the command fixture")
                        print(json.dumps({
                            "ok": True,
                            "path": "paced PCM capture → VAD → " + (
                                "keyword activation → one-breath command → " if args.wake else "command grammar → ")
                                + "confidence gate → deterministic alias parser → dry-run dispatcher → IPC feedback",
                            "elapsed_seconds": observed - started,
                            "activation_mode": "wake_word" if args.wake else "push_to_talk",
                            "wake_observed": wake_observed if args.wake else None,
                            "wake_phrase": cfg["activation"]["wake_phrase"] if args.wake else None,
                            "seconds_after_last_energy_frame_to_observed_dispatch": observed - float(endpoint.read_text()),
                            "endpoint_rms_threshold": args.endpoint_rms,
                            "endpoint_proxy_method": "Final command-fixture frame above fixed RMS threshold, selected before playback; timestamp is emission time plus 30ms frame duration.",
                            "timing_limit": "Synthetic paced replay; waveform energy is not human speech ground truth; pushed IPC status observation; not live microphone latency.",
                            "fixture_sha256": hashlib.sha256(audio).hexdigest(),
                            "microphone_used": False, "desktop_action_executed": False}, indent=2))
                        return
                    if status["state"] == "error":
                        raise RuntimeError(status["message"])
            raise RuntimeError("Expected replay command was not accepted within twelve seconds")
        finally:
            if subscriber:
                subscriber.close()
            if old_socket is None:
                os.environ.pop("JARVIS_SOCKET", None)
            else:
                os.environ["JARVIS_SOCKET"] = old_socket
            if daemon:
                if daemon.poll() is None:
                    daemon.send_signal(signal.SIGTERM)
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=2)
                daemon.stderr.close()


if __name__ == "__main__":
    main()
