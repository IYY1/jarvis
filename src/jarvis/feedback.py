"""Optional short local tones for recording state changes and saved replay clips."""
import math
import shutil
import struct
import subprocess


def play(state: str):
    from .host import is_windows
    if is_windows():
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND if state == "error" else winsound.MB_OK)
        return
    player = shutil.which("pw-play")
    if not player:
        return
    tones = {"recording": (880,), "idle": (660,), "error": (220,), "clip": (880, 1320)}.get(state, (440,))
    frames = 2400  # 100 ms per tone at 24 kHz; generated feedback, never microphone data
    pcm = b"".join(struct.pack("<h", int(1800 * math.sin(2 * math.pi * frequency * i / 24000)
                                       * min(1, i / 120, (frames - i) / 120))) for frequency in tones for i in range(frames))
    try:
        subprocess.run([player, "--raw", "--rate", "24000", "--channels", "1", "--format", "s16", "-"],
                       input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass  # optional feedback cannot break capture/finalization
