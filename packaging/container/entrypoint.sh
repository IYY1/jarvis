#!/bin/sh
# Jarvis build/test container entry point. See packaging/container/README.md.
set -eu
cd /src
inputs=/inputs
out=${JARVIS_OUTPUT:-/src/dist/windows}
# Keep container builds out of the checkout's own native/*/target folders.
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-/tmp/jarvis-target}

tests() {
  # Work on a clean copy so host-built binaries and target/ folders are ignored.
  rm -rf /tmp/work && mkdir /tmp/work
  tar -C /src --exclude='./native/*/target' --exclude=./src/jarvis/bin --exclude=./build --exclude=./dist \
    --exclude=./.venv --exclude='*/__pycache__' -cf - . | tar -C /tmp/work -xf -
  cd /tmp/work
  unset CARGO_TARGET_DIR
  python3 scripts/build_controller.py --test
  python3 scripts/build_cli.py --test
  python3 scripts/build_replay_store.py --test
  python3 scripts/build_voice_worker.py --test
  # Offline unit tests; model- and device-backed tests skip themselves.
  TMPDIR=/tmp PYTHONPATH=/tmp/work/src python3 -m unittest discover -s tests -q
  CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc \
    cargo build --release --locked --target x86_64-pc-windows-gnu --manifest-path native/voice-worker/Cargo.toml
  ls -l native/voice-worker/target/x86_64-pc-windows-gnu/release/jarvis-voice.exe
}

wine_python() {
  # Windows CPython and the hash-pinned wheels, installed once per prefix. The
  # python.org bootstrapper needs an X display even with /quiet.
  if [ ! -f "$WINEPREFIX/drive_c/JarvisPython/python.exe" ]; then
    xvfb-run -a sh -c "wineboot --init >/dev/null 2>&1; wineserver -w
      wine '$inputs/python-3.13.17-amd64.exe' /quiet InstallAllUsers=0 'TargetDir=C:\\JarvisPython' \
        Include_test=0 Include_doc=0 Include_launcher=0 Shortcuts=0 AssociateFiles=0 PrependPath=0; wineserver -w"
    wine 'C:\JarvisPython\python.exe' -m pip install --no-index --find-links "Z:$inputs/wheels" \
      --require-hashes -r 'Z:\src\packaging\windows\requirements-lock.txt'
    wineserver -w
  fi
}

case "${1:-test}" in
  test) tests ;;
  windows)
    wine_python
    python3 scripts/build_windows_release.py --wine-prefix "$WINEPREFIX" \
      --makensis "$(command -v makensis)" --nsis-dir /usr/share/nsis --xvfb "$(command -v Xvfb)" \
      --models "$inputs/models" --ffmpeg-tools "$inputs/ffmpeg-tools/bin" \
      --ffmpeg-source "$inputs/FFmpeg-Corresponding-Source.tar.xz" --output "$out"
    ;;
  *) echo "usage: jarvis-build test|windows" >&2; exit 2 ;;
esac
