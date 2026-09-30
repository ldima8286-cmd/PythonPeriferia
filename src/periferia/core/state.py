"""Where the daemon leaves a note of what the microphone is doing.

The tray icon in the roadmap wants to know whether the microphone is live
without talking to the daemon. A tray in Python costs a Qt dependency of
about 80 MB, which is a poor price for a dot in the corner, so the daemon
writes this small file instead and anything that cares can read it. The same
file is what `periferia status` prints.

It lives in the runtime directory because it is runtime state: it is stale the
moment the daemon dies, and the directory is cleaned up on logout anyway. A
file whose only job is to be true right now does not belong in the config
directory, and a stale one there would look like a real setting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
CLOSING = "closing"
LATCHED = "latched"
PANIC = "panic"

# Every state a note is allowed to hold. Readers compare against this rather
# than against their own spelling of the words.
ALL = frozenset({CLOSED, OPEN, CLOSING, LATCHED, PANIC})


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "periferia" / "state.json"


def write(state: str, *, source: str | None, pid: int | None = None) -> None:
    """Record the state. Never raises: the microphone matters more than the note."""
    if state not in ALL:
        # A note nobody can interpret is worse than no note. This is what let a
        # window sit on "not running" for a live microphone: the reader matched
        # a spelling the writer never produced. Refusing the typo here means a
        # reader only ever has to understand states that exist.
        log.warning("refusing to record unknown state %r", state)
        return
    payload: dict[str, Any] = {
        "state": state,
        "source": source,
        "pid": pid if pid is not None else os.getpid(),
        "changed_at": time.time(),
    }
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed, so a reader either sees the old note or
        # the new one and never a half-written file.
        tmp = path.with_suffix(".json.new")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except OSError as exc:
        log.debug("cannot record the state: %s", exc)


def read() -> dict[str, Any] | None:
    """The recorded state, or None if nothing has been recorded."""
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_running(note: dict[str, Any] | None) -> bool:
    """Whether the process that wrote the note is still there.

    The note outlives its author. The daemon clears it on a clean exit, but a
    crash, a kill, or a reboot leaves it behind, and a reader that trusts the
    file alone will go on reporting whatever the microphone was doing at the
    moment it died. A window that says the microphone is open on the strength
    of a note from a dead process is worse than one that says nothing.

    Signal 0 checks for existence without delivering anything. It reports
    PermissionError for a process that exists under another user, which is
    still running as far as this question is concerned. A recycled pid will
    read as alive; that needs a heartbeat to rule out, and the daemon does not
    write one.
    """
    if not note:
        return False
    pid = note.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def age(note: dict[str, Any] | None) -> float | None:
    """How long ago the note was written, in seconds."""
    if not note:
        return None
    since = note.get("changed_at")
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        return None
    return max(0.0, time.time() - since)


def clear() -> None:
    with contextlib.suppress(OSError):
        state_path().unlink()
