#!/usr/bin/env bash
# Install or update Jarvis on Arch Linux, CachyOS and other Arch-based systems.
#
#   curl -fsSL https://raw.githubusercontent.com/abdullaho21/jarvis/main/install.sh | bash
#
# Pass options after "bash -s --", e.g. ... | bash -s -- --gpu-recorder
# Run the same command again to update. Hyprland's config is never edited and
# the user service is written disabled; enable Start at login inside Jarvis.
set -euo pipefail

repo=${JARVIS_REPO:-https://github.com/abdullaho21/jarvis.git}
branch=${JARVIS_BRANCH:-main}
dir=${JARVIS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/jarvis}
link=$HOME/.local/bin/jarvis
gpu_recorder=0 native=1 models=() replace=() noconfirm=() uninstall=0

usage() {
    cat <<EOF
Usage: install.sh [options]

  --gpu-recorder   Also install gpu-screen-recorder (GPU formats, HDR, instant
                   replay). It adds a privileged KMS capture helper; see
                   docs/recording.md before enabling replay.
  --basic-models   Download only the small wake model (40 MB, lower accuracy)
                   instead of all voice models (about 680 MB).
  --python-only    Skip building the Rust voice worker, CLI and controller.
  --dir PATH       Install location (default: $dir).
  --branch NAME    Git branch to install (default: $branch).
  --replace        Overwrite an existing launcher, desktop entry or service
                   that this installer did not write. Review them first.
  --yes            Do not ask before installing packages.
  --uninstall      Remove Jarvis; settings, models and recordings are kept.
EOF
}

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

uninstall() {
    [ -x "$dir/run-jarvis" ] || die "no Jarvis installation at $dir"
    if systemctl --user cat murmur.service >/dev/null 2>&1; then
        systemctl --user disable --now murmur.service || true
    fi
    rm -f -- "$desktop" "$unit"
    systemctl --user daemon-reload 2>/dev/null || true
    [ "$(readlink -- "$link" 2>/dev/null)" != "$dir/run-jarvis" ] || rm -f -- "$link"
    rm -rf -- "$dir"
    say "Removed Jarvis. Settings, models and recordings remain in $data/murmur and $config/murmur."
}

install_packages() {
    # Ask pacman what is missing, so an existing PulseAudio or rustup install
    # satisfies the audio server and Cargo requirements.
    local packages=(git python python-gobject gtk4 python-setuptools pipewire wireplumber libpulse
                    ffmpeg wf-recorder gst-plugins-good base-devel cmake ninja pulse-native-provider)
    [ "$native" = 0 ] || packages+=(cargo)
    [ "$gpu_recorder" = 0 ] || packages+=(gpu-screen-recorder)
    local missing i tty=/dev/stdin
    mapfile -t missing < <(pacman -T "${packages[@]}" || true)
    for i in "${!missing[@]}"; do
        case ${missing[i]} in
            cargo) missing[i]=rust ;;
            pulse-native-provider) missing[i]=pipewire-pulse ;;
        esac
    done
    if [ ${#missing[@]} -eq 0 ]; then
        say "System packages are already installed"
        return
    fi
    say "Installing packages: ${missing[*]}"
    # Under "curl | bash" stdin is the script, so pacman's questions use the terminal.
    [ -t 0 ] || ! (: </dev/tty) 2>/dev/null || tty=/dev/tty
    # shellcheck disable=SC2024 # the user, not root, opens the terminal
    sudo pacman -S --needed "${noconfirm[@]}" "${missing[@]}" <"$tty" ||
        die "pacman failed. If a package was not found, update first with: sudo pacman -Syu"
}

link_command() {
    mkdir -p -- "$(dirname -- "$link")"
    local current
    current=$(readlink -- "$link" 2>/dev/null || true)
    if { [ ! -e "$link" ] && [ ! -L "$link" ]; } || [ "$current" = "$dir/run-jarvis" ] || [ ${#replace[@]} -gt 0 ]; then
        ln -sfn -- "$dir/run-jarvis" "$link"
    else
        warn "$link already exists and was left alone; pass --replace to point it at this install"
    fi
    case ":$PATH:" in
        *":$(dirname -- "$link"):"*) ;;
        *) warn "$(dirname -- "$link") is not on your PATH; add it to run 'jarvis' from a terminal" ;;
    esac
}

# Everything runs from main so "curl | bash" has read the whole script first.
main() {
    while [ $# -gt 0 ]; do
        case $1 in
            --gpu-recorder) gpu_recorder=1 ;;
            --basic-models) models=(--basic) ;;
            --python-only) native=0 ;;
            --dir) [ $# -ge 2 ] || die "--dir needs a path"; dir=$2; shift ;;
            --branch) [ $# -ge 2 ] || die "--branch needs a name"; branch=$2; shift ;;
            --replace) replace=(--replace) ;;
            --yes) noconfirm=(--noconfirm) ;;
            --uninstall) uninstall=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done

    [ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root; it asks for sudo only to install packages"
    command -v pacman >/dev/null || die "this installer is for Arch-based systems (pacman not found); see the README for other setups"
    dir=$(realpath -m -- "$dir")
    py=$dir/.venv/bin/python
    data=${XDG_DATA_HOME:-$HOME/.local/share}
    config=${XDG_CONFIG_HOME:-$HOME/.config}
    desktop=$data/applications/org.murmur.Controller.desktop
    unit=$config/systemd/user/murmur.service
    if [ "$uninstall" = 1 ]; then
        uninstall
        return
    fi

    install_packages
    command -v hyprctl >/dev/null || warn "Hyprland was not found; window, workspace and app commands need it"
    if [ "$native" = 1 ] && ! cargo --version >/dev/null 2>&1; then
        die "cargo has no Rust toolchain; run 'rustup default stable' or pass --python-only"
    fi

    if [ -d "$dir/.git" ]; then
        say "Updating $dir"
        git -C "$dir" pull --ff-only origin "$branch"
    elif [ -e "$dir" ] && [ -n "$(ls -A -- "$dir")" ]; then
        die "$dir exists and is not a Jarvis checkout; choose another location with --dir"
    else
        say "Downloading Jarvis into $dir"
        git clone --branch "$branch" -- "$repo" "$dir"
    fi

    # A Python upgrade leaves the venv's packages under the old version: rebuild it.
    local version
    version=$(python3 -c 'import sys; print(sys.version_info[:2])')
    if [ -x "$py" ] && [ "$("$py" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)" != "$version" ]; then
        say "Rebuilding the Python environment for the new system Python"
        rm -rf -- "$dir/.venv"
    fi
    [ -x "$py" ] || python3 -m venv --system-site-packages "$dir/.venv"
    say "Installing Jarvis and its voice libraries"
    "$py" -m pip install --disable-pip-version-check --quiet -e "${dir}[voice]"

    if [ "$native" = 1 ]; then
        say "Building the Rust voice worker, CLI and controller (the first build takes a few minutes)"
        local script
        for script in build_voice_worker build_cli build_controller; do
            "$py" "$dir/scripts/$script.py"
        done
    fi

    say "Downloading and verifying voice models"
    "$py" -m murmur install-model "${models[@]}"

    say "Adding the app launcher and the disabled user service"
    "$py" "$dir/scripts/install_user.py" --service "${replace[@]}"
    link_command

    cat <<EOF

Jarvis is installed in $dir.

  1. Open Jarvis from your app launcher, or run: jarvis
  2. In Privacy & startup, allow the microphone, then unmute.
  3. Say "hey Jarvis, open Firefox".

"Start at login" in Privacy & startup keeps Jarvis running in the background
when your session starts graphical-session.target (for example with uwsm).
See "Background startup" in $dir/README.md.

Run this installer again to update, or with --uninstall to remove Jarvis.
EOF
}

main "$@"
