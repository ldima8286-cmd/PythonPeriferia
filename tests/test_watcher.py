from __future__ import annotations

import os
from pathlib import Path

from src.periferia.core import watcher as watcher_mod


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_an_unchanged_file_does_not_call_back(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=0.0)

    assert w.check() is False
    assert w.check() is False
    assert calls == []


def test_a_rewrite_calls_back_once(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=0.0)

    _write(path, "a: 2\n")
    assert w.check() is True
    # The stamp was already taken, so polling again must stay quiet.
    assert w.check() is False
    assert calls == [1]


def test_the_interval_keeps_a_burst_down_to_one_reload(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=10.0)

    # The first check is always allowed through; the ones inside the interval
    # after it are not, so a burst of writes costs one reload.
    _write(path, "a: 2\n")
    assert w.check(now=0.0) is True
    assert w.check(now=1.0) is False
    assert w.check(now=9.9) is False
    assert calls == [1]

    _write(path, "a: 3\n")
    assert w.check(now=10.0) is True
    assert calls == [1, 1]


def test_a_new_file_of_the_same_size_in_the_same_tick_is_still_a_change(
    tmp_path: Path,
) -> None:
    """The reason size and inode are in the stamp, not just mtime."""
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=0.0)

    # Same byte count, and mtime is nudged back a second to where it was.
    _write(path, "a: 2\n")
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))

    assert w.check() is True
    assert calls == [1]


def test_a_callback_that_raises_is_contained(tmp_path: Path) -> None:
    """The config is re-read here; a broken one must not take the daemon with it."""
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")

    def boom() -> None:
        raise ValueError("bad config")

    w = watcher_mod.ConfigWatcher(path, boom, interval=0.0)
    _write(path, "a: 2\n")

    assert w.check() is False
    # The stamp still moved on, so the broken file is not retried every second.
    assert w.check() is False


def test_a_deleted_file_is_not_a_change_to_undo(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=0.0)

    path.unlink()
    assert w.check() is False
    assert calls == []


def test_forget_treats_the_current_file_as_the_starting_point(tmp_path: Path) -> None:
    """Our own rewrite is not a new change."""
    path = tmp_path / "config.yaml"
    _write(path, "a: 1\n")
    calls: list[int] = []
    w = watcher_mod.ConfigWatcher(path, lambda: calls.append(1), interval=0.0)

    _write(path, "a: 2\n")
    w.forget()
    assert w.check() is False
    assert calls == []


def test_there_is_no_watcher_without_a_file(tmp_path: Path) -> None:
    assert watcher_mod.config_watcher(None, lambda: None) is None
    assert watcher_mod.config_watcher(tmp_path / "nope.yaml", lambda: None) is None


def test_the_default_interval_is_about_a_second() -> None:
    assert 0.5 <= watcher_mod.CHECK_INTERVAL_S <= 5.0