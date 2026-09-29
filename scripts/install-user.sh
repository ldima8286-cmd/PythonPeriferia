#!/usr/bin/env bash
# Install into ~/.local without a virtualenv. Optional, the dev setup is enough.
set -euo pipefail

cd "$(dirname "$0")/.."

PREFIX="${PREFIX:-$HOME/.local}"
python3 -m venv --system-site-packages "$PREFIX/share/periferia/venv"

"$PREFIX/share/periferia/venv/bin/pip" install --upgrade pip
"$PREFIX/share/periferia/venv/bin/pip" install .

mkdir -p "$PREFIX/bin"
cat >"$PREFIX/bin/periferia" <<EOF
#!/bin/sh
exec "$PREFIX/share/periferia/venv/bin/periferia" "\$@"
EOF
cat >"$PREFIX/bin/periferia-daemon" <<EOF
#!/bin/sh
exec "$PREFIX/share/periferia/venv/bin/periferia-daemon" "\$@"
EOF
chmod +x "$PREFIX/bin/periferia" "$PREFIX/bin/periferia-daemon"

echo "installed into $PREFIX/bin"
echo "ensure PATH has $PREFIX/bin"
