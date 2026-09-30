"""Probe: can this machine tell us which window has focus?

Per-window profiles are the one feature in the roadmap that nothing else can be
built on top of, and on Wayland they are not guaranteed to be possible. This
module does not implement the feature. It finds out whether it is implementable
here, and reports which window fields would be usable for matching.

KWin is reached through its scripting interface: a small QJSEngine script is
handed to the compositor, which runs it inside the compositor's own process and
prints what it sees. Reading the result out of the compositor's log keeps the
project free of a D-Bus library, which the core deliberately does not have.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import windowbus
from .envcheck import FAIL, OK, WARN

MARKER = "IRONINPUT-WINDOW-PROBE"

SCRIPT_NAME = "periferiaProbe"

KWIN_SERVICE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_IFACE = "org.kde.kwin.Scripting"
SCRIPT_IFACE = "org.kde.kwin.Script"

SCRIPT = """
function report(stage, fields) {
    callDBus(
        "%(bus)s", "%(path)s", "%(iface)s", "Report",
        JSON.stringify({"stage": stage, "fields": fields})
    );
}

function active() {
    try {
        if (workspace.activeClient !== null && workspace.activeClient !== undefined) {
            return workspace.activeClient;
        }
    } catch (error) {
    }
    try {
        return workspace.activeWindow;
    } catch (error) {
        return null;
    }
}

function read(client, name) {
    try {
        var value = client[name];
        if (value === undefined) {
            return null;
        }
        if (value === null) {
            return null;
        }
        return "" + value;
    } catch (error) {
        return null;
    }
}

function basics(client) {
    return {
        "caption": read(client, "caption"),
        "resourceClass": read(client, "resourceClass"),
        "resourceName": read(client, "resourceName"),
        "windowRole": read(client, "windowRole"),
        "desktopFile": read(client, "desktopFile"),
        "internalId": read(client, "internalId")
    };
}

function send() {
    var client = active();
    if (client === null) {
        report("error", {"message": "no active client"});
        return;
    }
    report("basics", basics(client));
}

report("hello", {"ok": "1"});
send();

try {
    workspace.clientActivated.connect(send);
} catch (error) {
}
"""


@dataclass(slots=True)
class WindowReport:
    status: str
    detail: str
    hint: str = ""
    caption: str | None = None
    resource_class: str | None = None
    resource_name: str | None = None
    window_role: str | None = None
    desktop_file: str | None = None
    script_id: int | None = field(default=None)
    fields: dict[str, Any] = field(default_factory=dict)
    script_name: str = ""

    def matchable(self) -> list[str]:
        out = []
        if self.resource_class:
            out.append("resourceClass")
        if self.resource_name:
            out.append("resourceName")
        if self.window_role:
            out.append("windowRole")
        if self.desktop_file:
            out.append("desktopFile")
        return out

    def interesting(self) -> list[tuple[str, str]]:
        return [(name, value) for name, value in self._read() if value]

    def silent(self) -> list[str]:
        return [name for name, value in self._read() if not value]

    def _read(self) -> list[tuple[str, str | None]]:
        out = []
        for name, entry in self.fields.items():
            if isinstance(entry, dict):
                entry = entry.get("text")
            out.append((name, str(entry) if entry else None))
        return out


@dataclass(slots=True)
class BusReport:
    address: str
    is_flatpak_proxy: bool
    has_real_socket: bool
    detail: str


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def inspect_bus() -> BusReport:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    flatpak = "flatpak/bus" in address or "/run/flatpak/" in address
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    real_socket = os.path.join(runtime, "bus")
    has_real = False
    if os.path.exists(real_socket):
        target = os.path.realpath(real_socket)
        has_real = "/run/flatpak/" not in target
    if flatpak:
        detail = (
            "DBUS_SESSION_BUS_ADDRESS points at the Flatpak proxy, so the desktop "
            "session bus is not being addressed"
        )
    elif not address:
        detail = "DBUS_SESSION_BUS_ADDRESS is unset"
    else:
        detail = f"addressing {address}"
    return BusReport(address, flatpak, has_real, detail)


def _gdbus(
    method: str,
    *arguments: str,
    path: str = SCRIPTING_PATH,
    interface: str = SCRIPTING_IFACE,
    service: str = KWIN_SERVICE,
) -> subprocess.CompletedProcess[str]:
    args = [
        "gdbus",
        "call",
        "--session",
        "--dest",
        service,
        "--object-path",
        path,
        "--method",
        f"{interface}.{method}",
        *arguments,
    ]
    return _run(args)


def scripting_methods() -> set[str]:
    asked = _run(
        [
            "gdbus",
            "introspect",
            "--session",
            "--dest",
            KWIN_SERVICE,
            "--object-path",
            SCRIPTING_PATH,
        ]
    )
    return _parse_methods(asked.stdout)


def _parse_methods(introspection: str) -> set[str]:
    methods = set()
    for block in re.findall(r"<method\b.*?/>", introspection, re.S):
        found = re.search(r'name="([A-Za-z]+)"', block)
        if found:
            methods.add(found.group(1))
    return methods


def bus_names() -> list[str]:
    out = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.ListNames",
        ]
    )
    if out.returncode != 0:
        return []
    return re.findall(r"'([^']*)'", out.stdout)


def _kwin_log(since: str) -> str:
    out = _run(
        ["journalctl", "--user", "--since", since, "--no-pager", "-o", "cat"],
        timeout=15.0,
    )
    return out.stdout or ""


def _journal_sources(since: str) -> list[tuple[str, str]]:
    sources = [
        (
            "user journal",
            _run(
                ["journalctl", "--user", "--since", since, "--no-pager", "-o", "cat"],
                timeout=15.0,
            ).stdout
            or "",
        ),
        (
            "kwin tag",
            _run(
                ["journalctl", "--since", since, "--no-pager", "-o", "cat", "-t", "kwin"],
                timeout=15.0,
            ).stdout
            or "",
        ),
        (
            "kwin_wayland process",
            _run(
                [
                    "journalctl",
                    "--since",
                    since,
                    "--no-pager",
                    "-o",
                    "cat",
                    "_COMM=kwin_wayland",
                ],
                timeout=15.0,
            ).stdout
            or "",
        ),
    ]
    return sources


def diagnose(since: str, name: str = SCRIPT_NAME) -> list[str]:
    lines: list[str] = []
    loaded = _gdbus("isScriptLoaded", name)
    if loaded.returncode == 0:
        verdict = "yes" if "true" in loaded.stdout.lower() else "no"
        lines.append(f"isScriptLoaded({name}) -> {verdict}")
    else:
        text = (loaded.stderr or loaded.stdout).strip()
        lines.append(f"isScriptLoaded failed: {text.splitlines()[-1] if text else '?'}")

    for label, text in _journal_sources(since):
        if MARKER in text:
            lines.append(f"{label}: contains the marker")
            continue
        kwin_lines = [row for row in text.splitlines() if "kwin" in row.lower()]
        if kwin_lines:
            tail = kwin_lines[-1].strip()[:90]
            lines.append(f"{label}: {len(kwin_lines)} kwin lines, no marker, last: {tail}")
        else:
            lines.append(f"{label}: no kwin output at all")
    return lines


def _parse_marker(text: str) -> dict | None:
    for line in text.splitlines():
        index = line.find(MARKER)
        if index < 0:
            continue
        payload = line[index + len(MARKER) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "event" not in parsed:
            return parsed
    return None


def _text(fields: dict[str, Any], name: str) -> str | None:
    entry = fields.get(name)
    if isinstance(entry, dict):
        entry = entry.get("text")
    return str(entry) if entry else None


def _summarize(report: dict[str, Any], script_id: int | None) -> WindowReport:
    if report.get("stage") == "error":
        return WindowReport(
            WARN,
            f"KWin reported: {(report.get('fields') or {}).get('message', '?')}",
            "the compositor is alive but exposed no active client",
            script_id=script_id,
        )
    if report.get("error"):
        return WindowReport(
            WARN,
            f"KWin reported: {report['error']}",
            "the compositor is alive but exposed no active client",
            script_id=script_id,
        )
    fields = report.get("fields") or {}
    caption = _text(fields, "caption")
    resource_class = _text(fields, "resourceClass")
    resource_name = _text(fields, "resourceName")
    status = OK if resource_class or resource_name else WARN
    detail = (
        "KWin named the active window"
        if status == OK
        else "KWin named the window but sent no field a profile could match on"
    )
    hint = ""
    if status == OK:
        hint = (
            "caption changes with the window contents, so it cannot be what a "
            "profile matches on"
        )
    return WindowReport(
        status,
        detail,
        hint,
        caption=caption,
        resource_class=resource_class,
        resource_name=resource_name,
        window_role=_text(fields, "windowRole"),
        desktop_file=_text(fields, "desktopFile"),
        script_id=script_id,
        fields=fields,
    )


def _await_window(
    service: windowbus.WindowService, timeout: float, script_id: int | None
) -> tuple[dict[str, Any] | None, bool]:
    """Wait for the window report, treating the hello as liveness, not data."""
    deadline = time.monotonic() + timeout
    alive = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None, alive
        report = service.next_report(timeout=left)
        if report is None:
            return None, alive
        if report.get("stage") == "hello":
            alive = True
            continue
        return report, True


def _step_codes(steps: list[tuple[str, subprocess.CompletedProcess[str]]]) -> list[str]:
    out = []
    for label, result in steps:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        first = detail[0] if detail else "ok"
        out.append(f"{label} (exit {result.returncode}): {first}")
    return out


def probe(
    timeout: float = 12.0,
    on_report: Callable[[WindowReport], None] | None = None,
) -> WindowReport:
    bus = inspect_bus()
    if bus.is_flatpak_proxy and not bus.has_real_socket:
        return WindowReport(
            FAIL,
            "no real session bus reachable from here",
            "run this from your own terminal inside the Plasma session, not from a "
            "sandbox or a Flatpak",
        )
    if not shutil_which("gdbus"):
        return WindowReport(FAIL, "gdbus is not installed", "install glib2 tools")

    if not windowbus.HAVE_DBUS:
        return WindowReport(
            FAIL,
            "no D-Bus client, so the compositor has nowhere to report to",
            windowbus.MISSING,
        )

    since = f"-{int(timeout) + 5} seconds"
    service = windowbus.WindowService()
    name = f"{SCRIPT_NAME}{os.getpid()}"
    handle, path = tempfile.mkstemp(prefix="periferia-kwin-probe-", suffix=".js")
    try:
        if not service.start():
            return WindowReport(
                FAIL,
                "could not take a name on the session bus",
                f"{service.error}\n{windowbus.BUS_NAME}",
            )
        with os.fdopen(handle, "w") as stream:
            stream.write(
                SCRIPT
                % {
                    "marker": MARKER,
                    "bus": windowbus.BUS_NAME,
                    "path": windowbus.OBJECT_PATH,
                    "iface": windowbus.INTERFACE,
                }
            )
        _gdbus("unloadScript", name)
        loaded = _gdbus("loadScript", path, name)
        if loaded.returncode != 0:
            return _explain(loaded)
        script_id = _parse_int(loaded.stdout)
        steps = [("loadScript", loaded)]
        if script_id is not None and script_id < 0:
            held = _gdbus("isScriptLoaded", name)
            return WindowReport(
                FAIL,
                f"KWin refused the script and gave id {script_id}",
                "\n".join(
                    [
                        f"loadScript said {loaded.stdout.strip()}, which is how it"
                        f" says no",
                        f"isScriptLoaded({name}) -> "
                        f"{'yes' if held.returncode == 0 else 'no'}",
                        "a name that is already taken is the usual reason, and an"
                        " earlier run that exited early can leave one behind",
                        "run: qdbus6 org.kde.KWin /Scripting"
                        f" org.kde.kwin.Scripting.unloadScript {SCRIPT_NAME}",
                    ]
                ),
                script_name=name,
            )
        started = _gdbus("start")
        steps.append(("start", started))
        if started.returncode != 0:
            return _explain(started, "start")
        if "run" in scripting_methods():
            ran = _gdbus(
                "run",
                path=f"{SCRIPTING_PATH}/Script{script_id}",
                interface=SCRIPT_IFACE,
            )
            steps.append(("run", ran))
            if ran.returncode != 0:
                return _explain(ran, "run")
        report, alive = _await_window(service, timeout, script_id)
        if report is None:
            if alive:
                return WindowReport(
                    WARN,
                    "the script runs, but it cannot read the active window",
                    "\n".join(
                        [
                            "its hello arrived, so the script and the bus are fine; "
                            "reading the window is what fails",
                            *_step_codes(steps),
                            *diagnose(since, name),
                        ]
                    ),
                    script_id=script_id,
                )
            return WindowReport(
                WARN,
                "KWin took the script, but nothing came back over the bus",
                "\n".join(
                    [
                        "the script did not report, not even its own hello, so it "
                        "either failed to run or callDBus is not reaching us",
                        *_step_codes(steps),
                        *diagnose(since, name),
                    ]
                ),
                script_id=script_id,
            )
        first = _summarize(report, script_id)
        if on_report is not None:
            while True:
                with contextlib.suppress(KeyboardInterrupt):
                    later = service.next_report(timeout=3600.0)
                    if later is None:
                        break
                    on_report(_summarize(later, script_id))
        return first
    finally:
        _gdbus("unloadScript", name)
        with contextlib.suppress(OSError):
            os.unlink(path)
        service.stop()


def cleanup(script_id: int | None, script_name: str = SCRIPT_NAME) -> None:
    """Left for anything that loaded a script outside probe."""
    if script_id is not None or script_name:
        _gdbus("unloadScript", script_name or SCRIPT_NAME)


def _explain(
    failed: subprocess.CompletedProcess[str], step: str = "loadScript"
) -> WindowReport:
    text = (failed.stderr or failed.stdout).strip()
    reason = text.splitlines()[-1] if text else f"{step} failed"
    if "ServiceUnknown" in text or "not provided by any" in text:
        present = [
            name for name in bus_names() if "kde" in name.lower() or "kwin" in name.lower()
        ]
        hint = f"org.kde.KWin is not on the bus; kde names present: {present or 'none'}"
        return WindowReport(FAIL, "KWin is not reachable on this session bus", hint)
    if "UnknownObject" in text or "No such object" in text:
        return WindowReport(
            FAIL,
            f"KWin answered but has no scripting object: {reason}",
            f"check what it offers: gdbus introspect --session --dest {KWIN_SERVICE}",
        )
    if "AccessDenied" in text or "not authorized" in text:
        return WindowReport(FAIL, "KWin refused the call", reason)
    if "not a valid object path" in text or "is not a valid" in text:
        return WindowReport(
            FAIL,
            f"{step} was given a path KWin does not have",
            f"{reason}\nKWin 6 runs a loaded script from start() and has no such "
            f"per-script object, which is why {step} is not worth calling",
        )
    return WindowReport(FAIL, f"{step} failed: {reason}")


def _parse_int(text: str) -> int | None:
    for chunk in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
        if chunk.lstrip("-").isdigit():
            return int(chunk)
    return None


def shutil_which(name: str) -> bool:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return True
    return False
