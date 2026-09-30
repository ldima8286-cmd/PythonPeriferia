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
import select
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._window_receiver import BUS_NAME, INTERFACE, OBJECT_PATH
from .envcheck import FAIL, OK, WARN

MARKER = "IRONINPUT-WINDOW-PROBE"

SCRIPT_NAME = "periferiaProbe"

KWIN_SERVICE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_IFACE = "org.kde.kwin.Scripting"
SCRIPT_IFACE = "org.kde.kwin.Script"

SCRIPT = """
var sent = false;

function field(client, name) {
    try {
        var value = client[name];
        if (value === undefined || value === null) {
            return null;
        }
        return "" + value;
    } catch (error) {
        return null;
    }
}

function send() {
    if (sent) {
        return;
    }
    sent = true;
    var client = null;
    try {
        client = workspace.activeClient;
    } catch (error) {
        client = null;
    }
    if (client === null) {
        try {
            client = workspace.activeWindow;
        } catch (error) {
            client = null;
        }
    }
    if (client === null) {
        callDBus(
            "%(bus)s", "%(path)s", "%(iface)s", "Report",
            JSON.stringify({"error": "no active client"})
        );
        return;
    }
    var data = JSON.stringify({
        "caption": field(client, "caption"),
        "resourceClass": field(client, "resourceClass"),
        "resourceName": field(client, "resourceName"),
        "windowRole": field(client, "windowRole"),
        "desktopFile": field(client, "desktopFile"),
        "internalId": field(client, "internalId")
    });
    callDBus("%(bus)s", "%(path)s", "%(iface)s", "Report", data);
}

send();

try {
    workspace.clientActivated.connect(function () {
        sent = false;
        send();
    });
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


def diagnose(since: str) -> list[str]:
    lines: list[str] = []
    loaded = _gdbus("isScriptLoaded", SCRIPT_NAME)
    if loaded.returncode == 0:
        verdict = "yes" if "true" in loaded.stdout.lower() else "no"
        lines.append(f"isScriptLoaded({SCRIPT_NAME}) -> {verdict}")
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


RECEIVER = Path(__file__).with_name("_window_receiver.py")

GI_CHECK = (
    "import gi; gi.require_version('Gio','2.0');"
    "from gi.repository import Gio, GLib; print('ok')"
)


@dataclass(slots=True)
class Receiver:
    interpreter: str
    process: subprocess.Popen[str] | None = None
    note: str = ""
    error: str = ""

    def stop(self) -> None:
        if self.process is None:
            return
        with contextlib.suppress(Exception):
            self.process.terminate()
            self.process.wait(timeout=3)


def find_interpreter() -> Receiver:
    failures: list[str] = []
    tried: list[str] = []
    for candidate in (
        sys.executable,
        "/usr/bin/python3",
        "/usr/bin/python3.13",
        "python3",
    ):
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if not resolved or resolved in tried:
            continue
        tried.append(resolved)
        if not os.path.exists(resolved):
            failures.append(f"{resolved}: not present")
            continue
        out = _run([resolved, "-c", GI_CHECK], timeout=25.0)
        if out.returncode == 0 and "ok" in out.stdout:
            return Receiver(resolved, note="")
        detail = (out.stderr or out.stdout).strip().splitlines()
        failures.append(
            f"{resolved}: {detail[-1] if detail else 'PyGObject check failed'}"
        )
    if not tried:
        return Receiver("", error="no python interpreter was found at all")
    listed = "\n".join(failures)
    return Receiver("", error=f"no interpreter has PyGObject:\n{listed}")


def start_receiver(found: Receiver) -> WindowReport | None:
    process = subprocess.Popen(
        [found.interpreter, str(RECEIVER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    found.process = process
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        return WindowReport(FAIL, "the receiver exited before it was ready", found.error)
    try:
        first = json.loads(line)
    except json.JSONDecodeError:
        return WindowReport(FAIL, "the receiver printed something unexpected", line.strip()[:120])
    if not first.get("ready"):
        return WindowReport(
            FAIL,
            "the receiver could not take a name on the bus",
            str(first.get("error", "")) + f"\n{_gi_hint()}",
        )
    return None


def _gi_hint() -> str:
    return (
        "install the system PyGObject, then run this from a terminal, not a Flatpak: "
        "dnf install python3-gobject  (on Bazzite: rpm-ostree install python3-gobject, "
        "or use the discover/layer tooling your image provides)"
    )


def _read_report(process: subprocess.Popen[str], timeout: float) -> dict | None:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.25)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            return None
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "report" in parsed:
            report = parsed["report"]
            return report if isinstance(report, dict) else None
    return None


def probe(timeout: float = 12.0) -> WindowReport:
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

    since = f"-{int(timeout) + 5} seconds"
    found = find_interpreter()
    if found.error:
        return WindowReport(
            FAIL,
            "no usable PyGObject, so the compositor has nowhere to report to",
            f"{found.error}\n{_gi_hint()}",
        )

    receiver = start_receiver(found)
    handle, path = tempfile.mkstemp(prefix="periferia-kwin-probe-", suffix=".js")
    try:
        if receiver is not None:
            return receiver
        with os.fdopen(handle, "w") as stream:
            stream.write(
                SCRIPT
                % {
                    "marker": MARKER,
                    "bus": BUS_NAME,
                    "path": OBJECT_PATH,
                    "iface": INTERFACE,
                }
            )
        loaded = _gdbus("loadScript", path, SCRIPT_NAME)
        if loaded.returncode != 0:
            return _explain(loaded)
        script_id = _parse_int(loaded.stdout)
        _gdbus("start")
        if script_id is not None:
            _gdbus(
                "run",
                path=f"{SCRIPTING_PATH}/Script{script_id}",
                interface=SCRIPT_IFACE,
            )
        assert found.process is not None
        report = _read_report(found.process, timeout)
        if report is None:
            return WindowReport(
                WARN,
                "KWin took the script, but nothing came back over the bus",
                "\n".join(diagnose(since)),
                script_id=script_id,
            )
        if report.get("error"):
            return WindowReport(
                WARN,
                f"KWin reported: {report['error']}",
                "the compositor is alive but exposed no active client",
                script_id=script_id,
            )
        status = OK
        detail = "KWin named the active window"
        if not any(report.get(key) for key in ("resourceClass", "resourceName")):
            status = WARN
            detail = "KWin named the window but exposed no stable identifier"
        return WindowReport(
            status,
            detail,
            "caption changes with the window contents, so it cannot be what a "
            "profile matches on"
            if status == OK
            else "",
            caption=report.get("caption"),
            resource_class=report.get("resourceClass"),
            resource_name=report.get("resourceName"),
            window_role=report.get("windowRole"),
            desktop_file=report.get("desktopFile"),
            script_id=script_id,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
        found.stop()


def cleanup(script_id: int | None) -> None:
    if script_id is not None:
        _gdbus("unloadScript", SCRIPT_NAME)
        _gdbus("unloadScript", str(script_id))


def _explain(failed: subprocess.CompletedProcess[str]) -> WindowReport:
    text = (failed.stderr or failed.stdout).strip()
    reason = text.splitlines()[-1] if text else "loadScript failed"
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
    return WindowReport(FAIL, f"loadScript failed: {reason}")


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
