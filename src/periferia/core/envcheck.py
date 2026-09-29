"""Env check: answers "is this machine able to run Periferia at all".

Every check is read only, so this is safe to run any time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import pipewire

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(slots=True)
class Result:
    name: str
    status: str
    detail: str
    hint: str = ""


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def check_binaries() -> list[Result]:
    results = []
    for binary, critical in (("pactl", True), ("wpctl", True), ("pw-cli", False)):
        path = shutil.which(binary)
        results.append(
            Result(
                f"binary:{binary}",
                OK if path else (FAIL if critical else WARN),
                path or "not found",
                "install pipewire-utils" if not path else "",
            )
        )
    return results


def check_pipewire() -> Result:
    if not shutil.which("pactl"):
        return Result("pipewire", FAIL, "pactl missing", "install pipewire-utils")
    proc = _run(["pactl", "-f", "json", "info"])
    if proc.returncode != 0:
        return Result(
            "pipewire",
            FAIL,
            (proc.stderr or "cannot reach the sound server").strip(),
            "is pipewire-pulse running?",
        )
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Result("pipewire", WARN, "running, could not parse info")
    name = info.get("server_name") or "unknown"
    return Result("pipewire", OK, name)


def check_sources() -> list[Result]:
    if not shutil.which("pactl"):
        return [Result("sources", FAIL, "pactl missing", "")]
    try:
        items = pipewire.sources()
    except pipewire.PipeWireError as exc:
        return [Result("sources", FAIL, str(exc), "is pipewire-pulse running?")]

    if not items:
        return [Result("sources", FAIL, "no capture devices", "check the mic is connected")]

    results: list[Result] = []
    physical = 0
    virtual = 0
    for item in items:
        name = item.get("name") or "?"
        is_virtual = pipewire.is_virtual(name)
        virtual += int(is_virtual)
        physical += int(not is_virtual)
        tag = "virtual" if is_virtual else "physical"
        results.append(Result(f"source", OK, f"[{tag}] {name}"))

    if physical == 0:
        results.append(
            Result("sources", WARN, "no physical source found", "only virtual sources exist")
        )
    results.append(Result("sources:count", OK, f"{physical} physical, {virtual} virtual"))
    return results


def check_input_access() -> list[Result]:
    from ..modules.hotkey import list_input_devices

    results = []
    devices = list_input_devices()
    if not devices:
        return [
            Result(
                "input",
                FAIL,
                "no keyboard devices in /dev/input/by-id",
                "check that /dev/input/by-id exists",
            )
        ]
    readable = 0
    for path, name in devices:
        node = str(path)
        ok = os.access(node, os.R_OK | os.W_OK)
        readable += int(ok)
        results.append(
            Result(
                f"input:{name}",
                OK if ok else WARN,
                f"{node} {'readable' if ok else 'no access'}",
                "" if ok else "logind usually grants this; if not, see docs/udev.md",
            )
        )
    if readable == 0:
        results.append(Result("input", FAIL, "no device is accessible", "see docs/udev.md"))
    return results


def check_uinput() -> Result:
    path = "/dev/uinput"
    if not os.path.exists(path):
        return Result(
            "uinput", WARN, "/dev/uinput missing", "needed later for key remapping, not for PTT"
        )
    ok = os.access(path, os.W_OK)
    return Result(
        "uinput",
        OK if ok else WARN,
        f"{path} {'writable' if ok else 'not writable'}",
        "needed later for key remapping, not for PTT",
    )


def check_wayland() -> Result:
    if not os.environ.get("WAYLAND_DISPLAY"):
        if os.environ.get("DISPLAY"):
            return Result("session", WARN, "X11 session", "window tracking differs on Wayland")
        return Result("session", FAIL, "no DISPLAY or WAYLAND_DISPLAY", "")
    if os.environ.get("XDG_CURRENT_DESKTOP"):
        return Result("session", OK, f"Wayland / {os.environ['XDG_CURRENT_DESKTOP']}")
    return Result("session", OK, "Wayland")


def check_flatpak_sound() -> Result:
    """Flatpak apps need a pulse socket, not raw PipeWire."""
    sock = os.environ.get("XDG_RUNTIME_DIR", "")
    path = os.path.join(sock, "pulse", "native") if sock else ""
    if path and os.path.exists(path):
        return Result("flatpak-audio", OK, f"{path} present")
    return Result(
        "flatpak-audio",
        WARN,
        "no pulse socket in XDG_RUNTIME_DIR",
        "Flatpak apps may not see the sound server",
    )


def run_all() -> list[Result]:
    results: list[Result] = []
    results.append(check_wayland())
    results.extend(check_binaries())
    results.append(check_pipewire())
    results.extend(check_sources())
    results.extend(check_input_access())
    results.append(check_uinput())
    results.append(check_flatpak_sound())
    return results


SYMBOL = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]"}


def main() -> int:
    results = run_all()
    width = max((len(r.name) for r in results), default=10)
    for res in results:
        print(f"{SYMBOL[res.status]:<7} {res.name:<{width}}  {res.detail}")
        if res.hint and res.status != OK:
            print(f"{'':<7} {'':<{width}}  -> {res.hint}")

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    print()
    print(f"{len(failures)} failed, {len(warnings)} warnings")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
