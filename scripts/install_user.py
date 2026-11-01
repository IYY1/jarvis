#!/usr/bin/env python3
"""Install Jarvis's desktop launcher and optional user service.

Run with the Python environment in which jarvis-hyprland is installed. No root,
package installation, Hyprland edits, automatic service enable, or audio access.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def desktop_quote(value):
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return '"' + value + '"'


def systemd_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service", action="store_true", help="Also write the disabled user service")
    p.add_argument("--replace", action="store_true", help="Replace existing Jarvis-owned launcher/unit files")
    p.add_argument("--dry-run", action="store_true", help="Show proposed files without writing")
    a = p.parse_args()
    # Preserve venv interpreter path; resolving its symlink would lose installed dependencies.
    interpreter = str(Path(sys.executable).absolute())
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    files = {data / "applications/org.murmur.Controller.desktop": f'''[Desktop Entry]
Type=Application
Name=Jarvis
GenericName=Local voice controller
Comment=Control Hyprland by voice and configure screen recording
Exec={desktop_quote(interpreter)} -m murmur ui
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
Keywords=voice;Hyprland;microphone;recording;jarvis;assistant;
StartupNotify=true
Actions=Recorder;

[Desktop Action Recorder]
Name=Open recorder
Exec={desktop_quote(interpreter)} -m murmur ui --page recording
'''}
    if a.service:
        files[config / "systemd/user/murmur.service"] = f'''[Unit]
Description=Jarvis local voice controller
Documentation={(Path(__file__).resolve().parents[1] / 'README.md').as_uri().replace('%', '%%')}
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
Type=simple
Environment=MURMUR_MANAGED_SERVICE=1
ExecStart={systemd_quote(interpreter)} -m murmur daemon
Restart=on-failure
RestartSec=3
TimeoutStopSec=25
KillMode=mixed
UMask=0077

[Install]
WantedBy=graphical-session.target
'''
    # Validate all destinations before writing anything.
    for path, content in files.items():
        if path.exists() and path.read_text() != content and not a.replace:
            p.error(f"{path} already exists; review it or pass --replace")
    for path, content in files.items():
        print(path)
        if a.dry_run:
            print(content)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o644)
    if a.service and not a.dry_run:
        # Without a user session bus (SSH, su, containers) systemd reads the
        # unit at the next login instead; the files are already in place.
        if subprocess.run(["systemctl", "--user", "daemon-reload"]).returncode:
            print("Note: no user systemd session here; the service is picked up at your next login.",
                  file=sys.stderr)
    print("Preview only." if a.dry_run else "Installed for your user. Service enablement is unchanged; configure startup in Jarvis.")


if __name__ == "__main__":
    main()
