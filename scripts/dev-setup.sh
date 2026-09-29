#!/usr/bin/env bash
# Development setup: venv plus an editable install with the dev extras.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if ! command -v python3 >/dev/null; then
    echo "python3 not found" >&2
    exit 1
fi

VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "python: $VERSION"
if [ "$(printf '%s\n3.11\n3.12\n3.13\n' "$VERSION" | sort -V | head -1)" != "3.11" ]; then
    echo "warning: 3.11 or newer is required" >&2
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
    echo
    echo "python3-venv is missing. Install it:"
    echo "  sudo pacman -S python python-pip   # Arch"
    echo "  sudo apt install python3-venv    # Debian/Ubuntu"
    echo "  sudo dnf install python3         # Fedora"
    exit 1
fi

[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip
pip install -e ".[dev]"

echo
echo "ready. next:"
echo "  source .venv/bin/activate"
echo "  periferia check"
