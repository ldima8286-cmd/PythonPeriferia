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


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "periferia" / "state.json"


def write(state: str, *, source: str | None, pid: int | None = None) -> None:
    """Record the state. Never raises: the microphone matters more than the note."""
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


def clear() -> None:
    with contextlib.suppress(OSError):
        state_path().unlink()
