"""Build, verify and publish Jarvis 1.1 on a Windows GitHub Actions runner."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/release-1.1'
WORK.mkdir(parents=True, exist_ok=True)
OUTPUT = ROOT / 'dist/windows'

def run(*args, env=None):
    subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True)

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def download(url, path, expected=None):
    with urllib.request.urlopen(url, timeout=120) as src, path.open('wb') as dst:
        shutil.copyfileobj(src, dst)
    if expected and sha(path) != expected:
        raise RuntimeError('Download hash mismatch: ' + str(path))

run(sys.executable, '-m', 'pip', 'install', '--require-hashes', '-r', 'packaging/windows/requirements-lock.txt')
run(sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', '.')
run(sys.executable, '-m', 'unittest', 'tests.test_windows', 'tests.test_process_memory',
    'tests.test_voice_upgrade.FakeVoskEngineTests', 'tests.test_build_scripts', '-v')
archive = WORK / 'base.zip'
download('https://github.com/abdullaho21/jarvis/releases/download/v1.0.0/Jarvis-1.0.0-Windows-x64-Portable.zip',
         archive, '03ac560f5f20e39b77ed1ab21efae156f6f34ee9bef81f54b2024e99f4588a38')
with zipfile.ZipFile(archive) as src:
    src.extractall(WORK / 'assets')
base = WORK / 'assets/Jarvis'
manifest = json.loads((base / 'BUILD-MANIFEST.json').read_text())
for name, expected in manifest['files'].items():
    assert sha(base / name) == expected, name
nsis = WORK / 'nsis.zip'
download('https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip', nsis,
         'c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1')
with zipfile.ZipFile(nsis) as src:
    src.extractall(WORK)

# Import the installed MSVC/SDK environment without changing machine settings.
vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
vs = subprocess.check_output([str(vswhere), '-latest', '-products', '*', '-requires',
                              'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property',
                              'installationPath'], text=True).strip()
vsdev = Path(vs) / 'Common7/Tools/VsDevCmd.bat'
lines = subprocess.check_output(f'"{vsdev}" -arch=x64 -host_arch=x64 >nul && set', shell=True, text=True)
env = dict(os.environ)
for line in lines.splitlines():
    if '=' in line and not line.startswith('='):
        key, value = line.split('=', 1)
        env[key] = value
env['CARGO_TARGET_DIR'] = str(WORK / 'native')
env['RUSTFLAGS'] = '-Ctarget-feature=+crt-static'
sys.path.insert(0, str(ROOT / 'scripts'))
from native_build import without_build_paths
env = without_build_paths(env, ROOT)
run('rustup', 'toolchain', 'install', 'stable', '--profile', 'minimal')
for action in ('test', 'build'):
    run('cargo', '+stable', action, '--release', '--locked', '--jobs', '4', '--manifest-path',
        'native/voice-worker/Cargo.toml', env=env)
worker = WORK / 'native/release/jarvis-voice.exe'
run(worker, '--version')
sysroot = Path(subprocess.check_output(['rustc', '+stable', '--print', 'sysroot'], text=True).strip())
copyright_file = sysroot / 'share/doc/rust/COPYRIGHT-library.html'
if copyright_file.exists():
    shutil.copy2(copyright_file, base / 'licenses/voice-worker/Rust-standard-library.html')
run(sys.executable, 'scripts/build_windows.py', '--models', base / 'models', '--tools', base / 'tools',
    '--ffmpeg-source', base / 'FFmpeg-Corresponding-Source.tar.xz', '--extra-licenses', base / 'licenses',
    '--voice-worker', worker, '--replay-store', base / 'tools/jarvis-replay-store.exe',
    '--text-helper', base / 'tools/jarvis-type.exe', '--vosk-dir', base / 'tools', '--output', OUTPUT)
bundle = OUTPUT / 'frozen/Jarvis'
console = bundle / 'JarvisConsole.exe'
assert subprocess.check_output([str(console), '--version'], text=True).strip() == 'Jarvis 1.1.0'
doctor = json.loads(subprocess.check_output([str(console), 'doctor'], text=True))
assert doctor['frozen'] and doctor['voice_engine']['model_ready'] and doctor['voice_engine']['accurate_model_ready']
assert all(x['ok'] for x in doctor['tools'].values()) and all(doctor['dependencies'].values())
setup = OUTPUT / 'Jarvis-1.1.0-Windows-x64-Setup.exe'
run(WORK / 'nsis-3.11/makensis.exe', '/V2', f'/DBUNDLE={bundle}', '/DVERSION=1.1.0',
    f'/DOUTPUT={setup}', ROOT / 'packaging/windows/installer.nsi')
extracted = WORK / 'extracted'
run('7z', 'x', setup, f'-o{extracted}', '-y')
manifest = json.loads((extracted / 'BUILD-MANIFEST.json').read_text())
assert manifest['version'] == '1.1.0'
for name, expected in manifest['files'].items():
    assert sha(extracted / name) == expected, name
assert sha(extracted / 'tools/jarvis-voice.exe') == sha(worker)
assert (extracted / 'Uninstall.exe').is_file()
setup.with_suffix('.exe.sha256').write_text(sha(setup) + '  ' + setup.name + '\n')
assets = sorted(OUTPUT.glob('Jarvis-1.1.0-Windows-x64-*'))
assert len(assets) == 4, assets
(OUTPUT / 'SHA256SUMS').write_text(''.join(sha(p) + '  ' + p.name + '\n' for p in assets if p.suffix != '.sha256'))
assets.append(OUTPUT / 'SHA256SUMS')
notes = WORK / 'release-notes.md'
notes.write_text('''Windows update from PR #19, with original contributor commits preserved.

- Screenshots by voice, tray and settings, with PNG/JPG options.
- Up to six recording sound outputs and a separate recording microphone.
- Display selection survives Windows display renumbering.
- Faster wake-word handling, corrected clip joins and a new app icon.
- Fresh-install defaults enable wake listening, instant replay and starting with Windows; existing settings are retained.

Download the Windows x64 Setup.exe, or extract the entire portable ZIP. Both include offline English models, recording tools, runtime dependencies, license notices and FFmpeg corresponding source.

Validation: Windows policy/packaging tests and native worker tests pass; frozen offline dependency/model checks pass; every installer payload hash matches its build manifest after extraction. The Windows speech/capture worker is rebuilt from this release source. Unchanged replay and dictation helpers are from the verified official 1.0.0 release.

The installer is unsigned. Physical microphone, GPU/HDR, installation/upgrade/uninstallation and Windows 11 per-app audio were not tested by this release build.
''', encoding='utf-8')
# Create as a draft, attach every verified asset, then publish atomically to users.
run('gh', 'release', 'create', 'v1.1', '--repo', os.environ['GITHUB_REPOSITORY'], '--target',
    os.environ['GITHUB_SHA'], '--title', 'Jarvis 1.1', '--notes-file', notes, '--draft', *assets)
run('gh', 'release', 'edit', 'v1.1', '--repo', os.environ['GITHUB_REPOSITORY'], '--draft=false', '--latest')
