"""Follows the focused window on Plasma, and says which profile it wants.

Per-window profiles need to know what has focus, and on Wayland only the
compositor knows. On Plasma it will say so, if a script is given somewhere to say
it, and this is that: a QJSEngine script loaded into KWin, which reports the
active client over the session bus whenever the activation changes.

KWin only, on purpose. The general shape of this feature — a portal, or an
abstraction over GNOME, Mutter and KWin alike — is a much larger thing with three
times as many paths to be wrong, and the alternative to writing it is an honest
"not on this compositor" rather than a class that raises on the one desktop the
user actually has.

The compositor is asked through gdbus and reports over the bus rather than
through its log, for the reason in windowbus: a release Plasma prints nothing, so
silence there is indistinguishable from a script that threw.

Nothing in here decides which profile a window means. That is windowprofile's
job and it is pure, which is the only reason any of this could be tested.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any

from . import activewindow, windowbus, windowprofile

log = logging.getLogger(__name__)

SCRIPT_NAME = "periferiaWindow"

# The script reports the same window twice in quick succession often enough on a
# window switch that a rate limit is worth it: rebuilding a remap table grabs
# every keyboard, and doing that per event would make alt-tab stutter.
SETTLE_S = 0.25


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
        if (value === undefined || value === null) {
            return null;
        }
        return "" + value;
    } catch (error) {
        return null;
    }
}

function send() {
    var client = active();
    if (client === null) {
        report("basics", {"resourceClass": null, "resourceName": null, "caption": null});
        return;
    }
    report("basics", {
        "caption": read(client, "caption"),
        "resourceClass": read(client, "resourceClass"),
        "resourceName": read(client, "resourceName")
    });
}

report("hello", {"ok": "1"});
send();

try {
    workspace.clientActivated.connect(send);
} catch (error) {
}
"""


def report_to_window(report: dict[str, Any]) -> windowprofile.Window | None:
    """The bus message as a window, or None if it is not one.

    Kept apart from the bus so the parsing can be tested by writing messages
    rather than by convincing a compositor to send them.
    """
    if report.get("stage") != "basics":
        return None
    fields = report.get("fields")
    if not isinstance(fields, dict):
        return None

    def text(name: str) -> str | None:
        value = fields.get(name)
        if value is None:
            return None
        value = str(value)
        return value or None

    return windowprofile.Window(
        resource_class=text("resourceClass"),
        resource_name=text("resourceName"),
        caption=text("caption"),
    )


class WindowWatcher:
    """A loaded KWin script, polled rather than waited on.

    The daemon's loop already spins, so this exposes poll() instead of a thread
    of its own. A thread here would need its own locking against a daemon that
    rebuilds the keyboard from whichever side of it got there first.
    """

    def __init__(
        self,
        on_window: Callable[[windowprofile.Window], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.on_window = on_window
        self.error = ""
        self.started = False
        self._service: windowbus.WindowService | None = None
        self._script_name = ""
        self._script_path = ""
        self._clock = clock
        self._last: windowprofile.Window | None = None
        # A window seen but not yet applied, and when it stopped changing.
        self._pending: windowprofile.Window | None = None
        self._pending_since = 0.0
        self._pending_applied = True

    # -- starting and stopping -------------------------------------------------

    def start(self) -> bool:
        """Load the script. False with a reason in self.error.

        Refuses to start rather than reporting a failure later: a watcher that is
        not running and one that is running but silent look identical from the
        daemon, and the difference is whether switching windows does anything.
        """
        if not shutil.which("gdbus"):
            self.error = "gdbus is not installed (package glib2 tools)"
            return False
        if not windowbus.HAVE_DBUS:
            self.error = windowbus.MISSING
            return False

        service = windowbus.WindowService()
        if not service.start():
            self.error = f"could not take {windowbus.BUS_NAME} on the bus: {service.error}"
            return False

        self._script_name = f"{SCRIPT_NAME}{os.getpid()}"
        handle, path = tempfile.mkstemp(prefix="periferia-kwin-", suffix=".js")
        self._script_path = path
        with os.fdopen(handle, "w") as stream:
            stream.write(
                SCRIPT
                % {
                    "bus": windowbus.BUS_NAME,
                    "path": windowbus.OBJECT_PATH,
                    "iface": windowbus.INTERFACE,
                }
            )

        # A name left taken by a daemon that was killed stops this one from
        # loading, and the compositor says so with a negative id rather than an
        # error, so it is unloaded first on purpose.
        with contextlib.suppress(Exception):
            activewindow._gdbus("unloadScript", self._script_name, timeout=2.0)

        try:
            loaded = activewindow._gdbus("loadScript", path, self._script_name)
            if loaded.returncode != 0:
                self.error = (loaded.stderr or loaded.stdout).strip() or "loadScript failed"
                raise RuntimeError(self.error)
            script_id = activewindow._parse_int(loaded.stdout)
            if script_id is not None and script_id < 0:
                self.error = (
                    f"KWin refused the script and gave id {script_id}; the name "
                    f"{self._script_name} is probably already taken"
                )
                raise RuntimeError(self.error)

            started = activewindow._gdbus("start")
            if started.returncode != 0:
                self.error = (started.stderr or started.stdout).strip() or "start failed"
                raise RuntimeError(self.error)
            # KWin 5 needs an explicit run() after start(); KWin 6 has no such
            # method, which the probe already established.
            if "run" in activewindow.scripting_methods():
                ran = activewindow._gdbus(
                    "run",
                    path=f"{activewindow.SCRIPTING_PATH}/Script{script_id}",
                    interface=activewindow.SCRIPT_IFACE,
                )
                if ran.returncode != 0:
                    self.error = (ran.stderr or ran.stdout).strip() or "run failed"
                    raise RuntimeError(self.error)
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            self.stop()
            return False

        self._service = service
        self.started = True
        log.info("following the focused window as %s", self._script_name)
        return True

    def stop(self) -> None:
        """Unload the script. Idempotent, and never raises.

        A script left loaded in the compositor outlives the daemon and keeps
        reporting to a bus name nobody owns, which makes the next start look
        broken.
        """
        if self._script_name:
            with contextlib.suppress(Exception):
                activewindow._gdbus("unloadScript", self._script_name, timeout=2.0)
        self._script_name = ""
        if self._script_path:
            with contextlib.suppress(OSError):
                os.unlink(self._script_path)
            self._script_path = ""
        if self._service is not None:
            self._service.stop()
            self._service = None
        self.started = False

    # -- reading ---------------------------------------------------------------

    def drain(self) -> windowprofile.Window | None:
        """The focused window, once it has stopped changing. Never blocks.

        Drained rather than read one at a time because a burst of window changes
        should leave one answer, and the answer worth having is the last one.

        A window that has just been reported is held for SETTLE_S. KWin reports
        the same switch more than once often enough that answering each one would
        rebuild the remap table several times for one alt-tab, and every rebuild
        takes the keyboard back from the user for as long as it takes.
        """
        now = self._clock()
        if self._service is None:
            return None

        newest: dict[str, Any] | None = None
        while True:
            report = self._service.next_report(timeout=0)
            if report is None:
                break
            newest = report
        if newest is not None:
            window = report_to_window(newest)
            if window is not None and not _same_window(window, self._pending):
                self._pending = window
                self._pending_since = now
                self._pending_applied = False

        if self._pending is None or self._pending_applied:
            return None
        if now - self._pending_since < SETTLE_S:
            return None

        self._last = self._pending
        self._pending_applied = True
        return self._last

    @property
    def last(self) -> windowprofile.Window | None:
        return self._last


def wants_window(profiles: list[Any]) -> bool:
    """Whether any profile names a window, and so whether the watcher is needed.

    Without this the daemon would load a script into the compositor for a config
    that has one profile and never uses it, which is a thing to ask of a user's
    session for nothing.
    """
    return any(getattr(p, "match", None) for p in profiles)


def _same_window(left: windowprofile.Window, right: windowprofile.Window | None) -> bool:
    """Whether two reports describe the same window.

    KWin names the same window differently depending on what it knows, so a
    caption that appears and disappears with a taskbar is not a new window and
    must not restart the wait.
    """
    if right is None:
        return False
    return (left.resource_class, left.resource_name, left.caption) == (
        right.resource_class,
        right.resource_name,
        right.caption,
    )


def decode_report(raw: str) -> windowprofile.Window | None:
    """A bus message straight from JSON text. Used by the tests and the CLI."""
    try:
        report = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(report, dict):
        return None
    return report_to_window(report)