"""Which keyboards this machine has, and which one was used last.

The interesting property here is that a keyboard is remembered by something that
survives a reboot. `/dev/input/eventN` does not: the kernel numbers event nodes in
enumeration order, so the same USB keyboard comes back as event4 today and event7
after a reboot. Remembering the node would remember nothing, which is the bug that
made priority shuffling useless the first time around.

Everything else is deliberately forgiving. A corrupt or hand-edited cache is
ignored rather than fatal, a keyboard nobody has touched in half a year is
dropped, and a failed write is only logged: this file is a cache that makes the
common case nicer, and the daemon has to start without it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "keyboards.json"
# Looking for new keyboards means opening every candidate device to read its
# capabilities. That is too much to repeat on every idle poll, and keyboards
# arrive roughly never, so a few seconds is generous.
RESCAN_INTERVAL_S = 5.0
# A keyboard nobody has plugged in for six months is not coming back, and leaving
# it in the list makes the log harder to read than it needs to be.
FORGET_AFTER_DAYS = 180


def cache_path() -> Path:
    """Where the list is kept.

    Runtime directory, not config: this is a record of what was found rather
    than something the user chose, and a stale one in the config directory would
    look like a setting that ought to be there. The runtime directory is cleared
    on logout, which is the right lifetime for it.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "periferia" / FILE_NAME


def identity(path: Path, phys: str = "", by_id: str = "") -> str:
    """A name for a device that is the same one on the next boot.

    Physical path first. It is stable and unique per keyboard, which the by-id
    name is not always: a device without a serial number gets no by-id symlink
    at all, and two identical unserial keyboards would then collide. The by-id
    name is the fallback for anything the kernel does not give a physical path
    to, and the event node is the last resort, which is the case that cannot be
    fixed from here.
    """
    cleaned = phys.strip()
    if cleaned:
        return f"phys:{cleaned}"
    if by_id:
        return f"byid:{by_id}"
    return f"node:{path}"


def _load() -> list[dict[str, object]]:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("keyboards")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]


def _save(entries: list[dict[str, object]]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"keyboards": entries}, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.debug("cannot remember keyboards: %s", exc)


def _seen_at(entry: dict[str, object]) -> float:
    """When this keyboard was last in use, or 0 when the entry does not say.

    Anything that is not a number counts as no information. A hand-edited or
    half-written cache must not be allowed to decide the order, since a wrong
    answer here only means a different keyboard gets opened first.
    """
    seen = entry.get("last_seen")
    if isinstance(seen, bool) or not isinstance(seen, (int, float)):
        return 0.0
    return float(seen)


def _fresh(entry: dict[str, object]) -> bool:
    seen = _seen_at(entry)
    if not seen:
        return False
    return (time.time() - seen) < FORGET_AFTER_DAYS * 86400


class KnownKeyboards:
    """Keyboards seen on this machine, most recently used first.

    Order is the whole point of the class. `seen` puts a keyboard at the front,
    so the list reads as a usage order rather than an enumeration order, and
    `order` turns that into priorities for whichever devices happen to be
    plugged in right now.
    """

    def __init__(self, entries: list[dict[str, object]] | None = None) -> None:
        source = _load() if entries is None else entries
        self._entries: dict[str, dict[str, object]] = {}
        for entry in source:
            if not _fresh(entry):
                continue
            ident = entry["id"]
            assert isinstance(ident, str)
            self._entries[ident] = entry
        self._rescan_after: float = 0.0

    def seen(self, ident: str, *, node: Path | None = None) -> None:
        """Note this device is in use, which makes it the first priority."""
        previous = self._entries.get(ident, {})
        entry: dict[str, object] = {"id": ident, "last_seen": time.time()}
        name = previous.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name
        if node is not None:
            entry["node"] = str(node)
        rest = {k: v for k, v in self._entries.items() if k != ident}
        self._entries = {ident: entry, **rest}

    def touch(self, ident: str, name: str = "") -> None:
        """`seen`, plus a name to put in the log."""
        self.seen(ident)
        if name:
            entry = self._entries.get(ident)
            if entry is not None:
                entry["name"] = name

    def order(self, ids: list[str]) -> list[str]:
        """Priorities for `ids`: remembered ones by last use, then the rest as given.

        Unknown devices keep the order discovery found them in rather than being
        pushed to the end, so plugging in a brand new keyboard does not silently
        demote everything that was already working.
        """
        remembered = sorted(
            (i for i in ids if i in self._entries),
            key=lambda i: -_seen_at(self._entries[i]),
        )
        fresh = [i for i in ids if i not in self._entries]
        return remembered + fresh

    def known(self) -> list[dict[str, object]]:
        return sorted(self._entries.values(), key=lambda e: -_seen_at(e))

    def flush(self) -> None:
        _save(self.known())

    def rescan_due(self, now: float | None = None) -> bool:
        """Whether it is time to go looking for keyboards that were not there."""
        moment = time.monotonic() if now is None else now
        if moment < self._rescan_after:
            return False
        self._rescan_after = moment + RESCAN_INTERVAL_S
        return True