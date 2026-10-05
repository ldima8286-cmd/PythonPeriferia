"""Notices that the config file changed, so the daemon can act on it.

Recording a macro writes the config. Without this, `periferia macro record` is
followed by a daemon restart, which is the kind of step nobody reads and everybody
forgets, and the new macro sits there doing nothing until the next login.

Polling the file's modification time rather than watching it is a deliberate
choice. inotify needs a watch descriptor per file, and the config can be replaced
outright by an editor, which silently drops the watch and leaves the daemon
believing it is still looking at something. A stat() every few seconds cannot fail
in that way, and the cost is nothing at that interval.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# How often the config is checked. A macro recording writes it once at the end,
# so this only decides how long a finished recording waits before going live.
CHECK_INTERVAL_S = 1.0


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """mtime with nanoseconds, plus size, or None when the file is not there.

    Both parts matter. An editor that saves within the same second as the last
    write produces the same second-resolution mtime, and the change would be
    missed; size catches most of those. Nanoseconds alone catches almost all, and
    size is kept because it costs nothing.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


class ConfigWatcher:
    """Calls back when a file changes, with the first change being free.

    The first stamp is taken at construction, so starting the watcher never looks
    like a change. Without that the daemon would reload its own config the moment
    it started, and a reload rebuilds the keyboard listener.
    """

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], None],
        *,
        interval: float = CHECK_INTERVAL_S,
    ) -> None:
        self.path = path
        self._on_change = on_change
        self.interval = interval
        self._stamp = _stamp(path)
        self._next_check: float = 0.0

    def check(self, now: float | None = None) -> bool:
        """Reload-check if it is time. Returns whether it fired.

        A callback that raises must not kill the daemon. The microphone is open
        while this runs, and a config that cannot be parsed is the most likely
        reason for anything here to fail, which is exactly when the daemon most
        needs to keep going.
        """
        moment = time.monotonic() if now is None else now
        if moment < self._next_check:
            return False
        self._next_check = moment + self.interval

        current = _stamp(self.path)
        if current == self._stamp:
            return False

        if current is None:
            # A vanished file is what a rename-into-place looks like for an
            # instant, so nothing is reloaded. The new file is picked up on the
            # next tick, and if it never comes back the daemon keeps working on
            # the config it already loaded, which is better than dropping every
            # key binding because the file was moved.
            log.info("config file is gone, keeping what is loaded")
            self._stamp = None
            return False

        self._stamp = current

        log.info("config file changed, reloading")
        try:
            self._on_change()
        except Exception as exc:
            log.error("cannot apply the new config, keeping the old one: %s", exc)
            return False
        return True

    def forget(self) -> None:
        """Take the current file as the starting point, without reloading.

        Used after a reload that rewrote the file, where the change was ours and
        a second reload would only redo the same work.
        """
        self._stamp = _stamp(self.path)


def config_watcher(
    path: Path | None, on_change: Callable[[], None], *, interval: float = CHECK_INTERVAL_S
) -> ConfigWatcher | None:
    """A watcher for `path`, or None when there is no file to watch.

    The daemon is started from a systemd unit and from terminals alike, and a
    config path that does not exist yet is normal on a fresh install. Returning
    None keeps the caller from having to care.
    """
    if path is None:
        return None
    if not path.exists():
        log.debug("no config at %s yet, nothing to watch", path)
        return None
    return ConfigWatcher(path, on_change, interval=interval)


__all__ = ["CHECK_INTERVAL_S", "ConfigWatcher", "config_watcher"]
