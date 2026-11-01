#!/usr/bin/env python3
"""Build the Rust replay disk-store helper; no capture or service is started."""
import os
import sys
if __package__:
    from .native_build import build
else:
    from native_build import build

if __name__ == "__main__":
    if sys.platform == "win32":
        # A portable helper must not require a separately installed VC runtime.
        os.environ["RUSTFLAGS"] = os.environ.get("RUSTFLAGS", "") + " -Ctarget-feature=+crt-static"
    build(__doc__, [("replay-store", "jarvis-replay-store")],
          "The Windows disk-replay adapter can now use this checkout's helper.")
