"""Screen lock detection, so a held key cannot leave the microphone open.

The design calls for a forced release when the session locks. Without it,
walking away from a desk while holding PTT leaves the microphone transmitting
in an empty room, and a screen lock makes that the most likely way it happens.

logind announces this over D-Bus as org.freedesktop.login1.Session.LockHint.
`gdbus monitor` is used rather than a Python D-Bus binding so the project keeps
no extra dependency. If gdbus is missing or the session cannot be reached, the
watcher reports that and the daemon carries on without it: losing this
protection must never cost the user their PTT.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
from collections.abc import Callable

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.login1"
MANAGER_PATH = "/org/freedesktop/login1"
SESSION_IFACE = "org.freedesktop.login1.Session"
GET_SESSION = "org.freedesktop.login1.Manager.GetSession"
# logind is a system service. Asking for it on the session bus always fails
# with "The name is not activatable", which is what silently turned screen
# lock protection off on a machine that has it.
BUS_FLAG = "--system"


def parse_lock_hint(line: str) -> bool | None:
    """Read a LockHint value out of one line of gdbus monitor output.

    Lines look like:
      /org/freedesktop/login1/session/_32: org.freedesktop.login1.Session.LockHint: true
    Returns None for anything that is not a LockHint line, so unrelated signal
    traffic is ignored rather than guessed at.
    """
    if "LockHint" not in line:
        return None
    # the value is preceded by a colon, which strip() alone does not remove
    tail = line.rsplit("LockHint", 1)[-1].strip().strip(":").strip().lower()
    if tail == "true":
        return True
    if tail == "false":
        return False
    return None


def session_object_path(command: str = "gdbus") -> str | None:
    """Ask logind for this session's object path.

    The name contains a leading underscore, for example _32 for session 32,
    so it cannot be guessed and has to be asked for.
    """
    session_id = os.environ.get("XDG_SESSION_ID", "")
    if not session_id:
        log.info("no XDG_SESSION_ID, screen lock detection is unavailable")
        return None
    try:
        proc = subprocess.run(
            [
                command,
                "call",
                BUS_FLAG,
                "--dest",
                BUS_NAME,
                "--object-path",
                MANAGER_PATH,
                "--method",
                GET_SESSION,
                session_id,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("cannot reach logind for the session path: %s", exc)
        return None
    if proc.returncode != 0:
        log.info("logind GetSession failed: %s", (proc.stderr or "").strip())
        return None
    out = proc.stdout.strip().strip("()")
    parts = [p.strip().strip("'\"") for p in out.split(",")]
    if not parts or not parts[0].startswith("/"):
        return None
    return parts[0]


class SessionLockWatcher:
    """Watches LockHint and calls back when the session locks or unlocks."""

    def __init__(
        self,
        on_lock: Callable[[], None],
        *,
        command: str = "gdbus",
    ) -> None:
        self.on_lock = on_lock
        self.command = command
        self._proc: subprocess.Popen[str] | None = None
        self._locked = False

    @property
    def available(self) -> bool:
        return self._proc is not None

    def start(self) -> bool:
        """Begin watching. Returns False when it could not be set up."""
        path = session_object_path(self.command)
        if path is None:
            return False
        try:
            self._proc = subprocess.Popen(
                [
                    self.command,
                    "monitor",
                    BUS_FLAG,
                    "--dest",
                    BUS_NAME,
                    "--object-path",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log.info("cannot start gdbus monitor: %s", exc)
            self._proc = None
            return False
        log.info("watching screen lock on %s", path)
        return True

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def poll(self, timeout: float = 0.0) -> None:
        """Drain pending signal lines. Safe to call from the main loop."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                readable, _, _ = select.select([proc.stdout], [], [], timeout)
            except (OSError, ValueError):
                self._fail()
                return
            if not readable:
                return
            line = proc.stdout.readline()
            if not line:
                self._fail()
                return
            locked = parse_lock_hint(line)
            if locked is None:
                continue
            self._handle(locked)

    def _handle(self, locked: bool) -> None:
        if locked == self._locked:
            return
        self._locked = locked
        if locked:
            log.warning("session locked, forcing the microphone closed")
            self.on_lock()

    def _fail(self) -> None:
        log.info("screen lock watcher stopped, protection is off")
        self.stop()
