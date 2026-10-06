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
    # pactl is the only one the project actually calls. wpctl ships in the same
    # package but is never invoked, and demanding it only makes people install
    # things for no reason.
    for binary, critical in (("pactl", True), ("pw-cli", False)):
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
        results.append(Result("source", OK, f"[{tag}] {name}"))

    if physical == 0:
        results.append(
            Result("sources", WARN, "no physical source found", "only virtual sources exist")
        )
    results.append(Result("sources:count", OK, f"{physical} physical, {virtual} virtual"))
    return results


def _looks_like_mouse(name: str) -> bool:
    """Best guess from the by-id name, used only when the node is unreadable.

    A udev rule is written for the keyboards, so the mouse's extra input
    interfaces stay blocked on purpose. Reporting that as a warning made a
    correctly configured machine look half broken.
    """
    lowered = name.lower()
    return "mouse" in lowered or "pointer" in lowered


def _is_keyboard(dev: object) -> bool:
    # delegates, so "periferia check" and the daemon agree on what a keyboard is
    from ..modules.hotkey import is_keyboard

    return is_keyboard(dev)


def check_input_access() -> list[Result]:
    from ..modules.hotkey import list_input_devices, open_device

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

    results: list[Result] = []
    keyboards_ok = 0
    keyboards_seen = 0
    keyboards_blocked: list[str] = []
    other_blocked = 0

    for path, name in devices:
        try:
            dev = open_device(path)
        except Exception as exc:
            if _looks_like_mouse(name):
                # Expected to be blocked, and not a problem for PTT.
                other_blocked += 1
                continue
            keyboards_seen += 1
            keyboards_blocked.append(name)
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            results.append(Result(f"input:{name}", WARN, f"cannot open: {reason}"))
            continue

        keyboard = _is_keyboard(dev)
        dev.close()
        if keyboard:
            keyboards_seen += 1
            keyboards_ok += 1
            results.append(Result(f"input:{name}", OK, "openable for reading"))
        else:
            results.append(Result(f"input:{name}", OK, "openable for reading, not a keyboard"))

    if keyboards_ok:
        if other_blocked:
            results.append(
                Result(
                    "input",
                    OK,
                    f"{keyboards_ok} keyboard(s) readable, "
                    f"{other_blocked} non-keyboard device(s) blocked",
                    "blocked non-keyboard devices are expected and do not affect PTT",
                )
            )
    else:
        results.append(
            Result(
                "input",
                FAIL,
                "no keyboard can be opened"
                if keyboards_seen
                else "no device can be opened",
                "evdev cannot borrow logind access; a udev rule is required, see docs/udev.md",
            )
        )
        if keyboards_blocked:
            results.append(
                Result("input:blocked", WARN, ", ".join(keyboards_blocked))
            )
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


def check_graph() -> Result:
    """Whether the graph can be read directly from pw-dump.

    Only cleanup reads it, and there is a pactl answer behind it, so a missing
    pw-dump costs a slower cleanup rather than a broken microphone. Warn so
    the trade is visible without failing the check.
    """
    if not pipewire.have_graph():
        return Result(
            "graph",
            WARN,
            "pw-dump not found",
            "cleanup falls back to pactl, install pipewire-utils",
        )
    try:
        graph = pipewire.dump(timeout=5.0)
    except pipewire.PipeWireError as exc:
        return Result(
            "graph",
            WARN,
            str(exc),
            "cleanup falls back to pactl",
        )
    duplicates = graph.duplicates()
    detail = f"{len(graph.nodes)} nodes, {len(graph.links)} links"
    if duplicates:
        names = ", ".join(sorted(duplicates))
        return Result(
            "graph",
            WARN,
            f"{detail}; {len(duplicates)} duplicated name(s): {names}",
            "more than one node answers to a name, unload the stale module",
        )
    return Result("graph", OK, detail)


def run_all() -> list[Result]:
    results: list[Result] = []
    results.append(check_wayland())
    results.extend(check_binaries())
    results.append(check_pipewire())
    results.append(check_graph())
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
