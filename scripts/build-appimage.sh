#!/usr/bin/env bash
# Build a single-file AppImage.
#
# The shape of this is dictated by one fact: an AppImage is a squashfs with a
# runtime prepended, so the payload is compressed and can never be changed in
# place. Bundling the interpreter and PySide6 separately would mean shipping a
# runtime plus a payload plus the glue between them, so instead one frozen
# executable is built first and the image wraps that. One file out, and the
# bundled Python stays compressed alongside it.
#
# Run this on a machine with network access. It needs to download
# appimagetool and PyInstaller, neither of which is vendored here.

set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
VERSION="${VERSION:-0.1.0}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is missing. $2"; }

need python3 "Install a Python 3.11 or newer."
need curl "Install curl."
need squashfuse "On Fedora: sudo dnf install squashfuse. Needed to mount the finished image for testing."

say "checking the working tree is clean"
if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
    die "Uncommitted changes. A build should be reproducible from a commit."
fi

say "installing build tools into .venv-build"
VENV="$ROOT/.venv-build"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyinstaller appimagetool

say "freezing the application"
rm -rf "$BUILD"
mkdir -p "$BUILD"
# shellcheck disable=SC2046
(cd "$ROOT/packaging" && "$VENV/bin/pyinstaller" --noconfirm --clean \
    --distpath "$BUILD/dist" \
    --workpath "$BUILD/work" \
    --specpath "$BUILD" \
    --name periferia-gui \
    "$ROOT/packaging/periferia.spec")

FROZEN="$BUILD/dist/periferia-gui"
[ -x "$FROZEN" ] || die "PyInstaller produced no executable."

say "checking the frozen build actually starts"
# A binary that cannot start is the one failure this whole pipeline makes more
# likely, because excludes in the spec can remove something Qt loads lazily.
# Refuse to package one that does not.
if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$FROZEN" --version >/dev/null 2>&1 || die "Frozen build failed to start under xvfb."
else
    printf '\033[33m%s\033[0m\n' "xvfb-run not found, skipping the startup check. Install xvfb to have one."
fi

say "assembling the AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
install -m755 "$FROZEN" "$APPDIR/usr/bin/periferia-gui"
install -m755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
install -m644 "$ROOT/packaging/periferia.desktop" "$APPDIR/periferia.desktop"
install -m644 "$ROOT/packaging/icon/periferia.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/periferia.svg"
mkdir -p "$APPDIR/usr/share/metainfo"
if [ -f "$ROOT/packaging/periferia.appdata.xml" ]; then
    install -m644 "$ROOT/packaging/periferia.appdata.xml" \
        "$APPDIR/usr/share/metainfo/periferia.appdata.xml"
fi

say "producing the image"
OUTDIR="${OUTDIR:-$ROOT/dist}"
mkdir -p "$OUTDIR"
ARCH="$(uname -m)"
"$VENV/bin/appimagetool" \
    --no-appstream \
    "$APPDIR" \
    "$OUTDIR/Periferia-${VERSION}-${ARCH}.AppImage"

say "done: $OUTDIR/Periferia-${VERSION}-${ARCH}.AppImage"
printf '\033[2mMake it runnable with: chmod +x %s\n' "$OUTDIR/Periferia-${VERSION}-${ARCH}.AppImage"
printf '\033[2mThen install it with: --appimage-extract-and-run to test without extracting.\033[0m\n'
