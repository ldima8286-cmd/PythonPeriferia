#!/usr/bin/env bash
# Development setup: venv plus an editable install with the dev extras.
#
# On Fedora Atomic / Bazzite dnf cannot install software, and python3-devel is
# not in the base image, so evdev (a C extension) cannot be compiled. uv solves
# this: it brings its own CPython, headers included, and evdev has wheels for it.
set -euo pipefail

cd "$(dirname "$0")/.."

# Runtime dependency check, with the package name for the distro in use. The
# names differ and guessing wrong sends people to the wrong command.
check_runtime_deps() {
    local missing=0
    if ! command -v pactl >/dev/null 2>&1; then
        missing=1
        warn "pactl not found, needed to control the sound server"
        cat >&2 <<'EOF'

  Fedora, Bazzite, RHEL:   sudo dnf install pipewire-utils
  Arch:                    sudo pacman -S pipewire-utils
  Debian, Ubuntu, Mint:    sudo apt install pipewire-utils
  openSUSE:                sudo zypper install pipewire-utils
  Alpine:                  sudo apk add pipewire-utils

EOF
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found, the daemon cannot be installed as a service here"
    fi
    [ "$missing" -eq 0 ] || true
}

is_atomic() {
  [ -e /run/ostree-booted ] || command -v rpm-ostree >/dev/null 2>&1
}

say() { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$1" >&2; }

check_runtime_deps

ATOMIC=0
if is_atomic; then
    ATOMIC=1
    say "immutable system detected (rpm-ostree / Bazzite)"
fi

# uv ships its own interpreter, so it needs nothing from the system.
if command -v uv >/dev/null 2>&1; then
    say "using uv"
    [ -d .venv ] || uv venv --python 3.13 .venv
    say "installing"
    uv pip install --python .venv/bin/python -e ".[dev]"
else
    if [ "$ATOMIC" -eq 1 ]; then
        cat >&2 <<'EOF'

error: uv is not installed, and this system needs it.

The system python cannot build evdev here: its development headers
(Python.h) are not part of an immutable base image.

Install uv into ~/.local, then rerun this script:

  curl -LsSf https://astral.sh/uv/install.sh | sh
  rm -rf .venv && bash scripts/dev-setup.sh

To make uv visible right now without a new shell:

  export PATH="$HOME/.local/bin:$PATH"

The alternative is to add the headers to the system, which changes the
OS image and needs a reboot:

  sudo rpm-ostree install python3-devel
  sudo systemctl reboot
EOF
        exit 1
    fi

    say "using the system python"
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 not found" >&2
        exit 1
    fi

    VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    echo "python: $VERSION"

    if ! python3 -c 'import sysconfig, pathlib; pathlib.Path(sysconfig.get_paths()["include"], "Python.h").exists()'; then
        warn "python development headers are missing (Python.h not found)"
        cat >&2 <<'EOF'

evdev is a C extension and cannot be built without them.

Options, easiest first:

  1. install uv, it brings its own interpreter:
       curl -LsSf https://astral.sh/uv/install.sh | sh

  2. on a normal Fedora box, install the headers:
       sudo dnf install python3-devel

  3. on Fedora Atomic / Bazzite use rpm-ostree instead of dnf:
       sudo rpm-ostree install python3-devel
       sudo systemctl reboot

  4. on other distributions:
       sudo apt install python3-dev        # Debian, Ubuntu, Mint
       sudo pacman -S python               # Arch
       sudo zypper install python3-devel    # openSUSE
       sudo apk add python3-dev            # Alpine
       sudo emerge dev-python/python        # Gentoo
EOF
        exit 1
    fi

    [ -d .venv ] || python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"
fi

say "verifying"
.venv/bin/python -c "import evdev, yaml; print('evdev', evdev.__version__ if hasattr(evdev, '__version__') else 'ok'); print('yaml ok')"

cat <<'EOF'

ready. next:
  source .venv/bin/activate
  periferia check
EOF
