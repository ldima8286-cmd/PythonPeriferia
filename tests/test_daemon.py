"""Tests for the daemon's startup and shutdown.

The failure these cover is not visible from the outside: a run that cannot open
the input device returns straight out, and anything the module had already
loaded stays loaded in PipeWire. The next run then finds a source with the same
name, pactl resolves that name to whichever came first, and the gate drives one
node while everything else reads the other. The symptom is a microphone whose
level never moves, no matter what key is pressed.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from src.periferia.core import config as config_mod

daemon_mod = importlib.import_module("periferia.core.daemon")
Daemon = daemon_mod.Daemon


class _Listener:
    def __init__(self, *, fails: bool = False, stop_after: Any = None) -> None:
        self.fails = fails
        self.stop_after = stop_after
        self.polls = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        if self.fails:
            raise PermissionError("no permission")
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def poll(self, timeout: float = 0.0) -> None:
        self.polls += 1
        if self.stop_after is not None:
            self.stop_after()


class _Mic:
    source = "echo-cancel-source"

    def __init__(self) -> None:
        self.silenced = False
        self.torn_down = False

    def force_silence(self) -> None:
        self.silenced = True

    def teardown(self) -> None:
        self.torn_down = True

    def open_mic(self) -> None:
        pass

    def close_mic(self) -> None:
        pass

    def panic(self) -> None:
        pass


class _Processing:
    def __init__(self, _cfg: Any) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _daemon(listener: _Listener) -> Daemon:
    cfg = config_mod.Config()
    cfg.ptt.release_on_lock = False
    daemon = Daemon(cfg)
    daemon.mic = _Mic()  # type: ignore[assignment]
    daemon.processing = _Processing(cfg.processing)  # type: ignore[assignment]
    daemon._listener = listener  # type: ignore[assignment]
    daemon.setup = lambda: True  # type: ignore[method-assign]
    return daemon


def test_a_failed_device_open_still_unloads_the_module() -> None:
    listener = _Listener(fails=True)
    daemon = _daemon(listener)

    assert daemon.run() == 1

    assert daemon.processing.stopped, "the module was leaked on a failed start"
    assert daemon.mic.torn_down  # type: ignore[attr-defined]
    assert not listener.closed, "nothing was opened, so nothing may be closed"


def test_a_normal_shutdown_cleans_up_in_order() -> None:
    holder: dict[str, Daemon] = {}
    listener = _Listener(stop_after=lambda: holder["d"]._stop.set())
    daemon = _daemon(listener)
    holder["d"] = daemon

    assert daemon.run() == 0

    assert listener.polls >= 1, "the loop should have run at least once"
    assert listener.opened and listener.closed
    assert daemon.processing.stopped
    assert daemon.mic.silenced  # type: ignore[attr-defined]
    assert daemon.mic.torn_down  # type: ignore[attr-defined]


def test_a_stop_request_ends_the_loop() -> None:
    listener = _Listener()
    daemon = _daemon(listener)
    daemon._stop.set()

    assert daemon.run() == 0
    assert daemon.processing.stopped


def test_setup_failure_needs_no_cleanup() -> None:
    # nothing was loaded, so there is nothing to unload and nothing to close
    listener = _Listener()
    daemon = _daemon(listener)
    daemon.setup = lambda: False  # type: ignore[method-assign]

    assert daemon.run() == 1
    assert not listener.opened
    assert not daemon.processing.stopped


@pytest.mark.parametrize("event", ["press", "release", "panic", "lock"])
def test_state_changes_are_recorded_for_callers(event: str) -> None:
    # the self check reads these instead of guessing from the output level
    daemon = _daemon(_Listener())
    before = len(daemon.events(event))

    getattr(daemon, {"lock": "on_session_locked"}.get(event, f"on_{event}"))()

    assert len(daemon.events(event)) == before + 1


def test_events_ignore_what_happened_before_the_caller_looked() -> None:
    daemon = _daemon(_Listener())
    daemon.on_panic()
    marked = daemon.events("panic")[0]

    assert daemon.events("panic", since=marked - 1.0) == [marked]
    assert daemon.events("panic", since=marked + 1.0) == []
    assert daemon.events("release", since=0.0) == []
