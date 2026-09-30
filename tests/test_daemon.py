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
import logging
import sys
import time
from typing import Any

import pytest

from src.periferia.core import config as config_mod
from src.periferia.core import state as state_mod

daemon_mod = importlib.import_module("periferia.core.daemon")
Daemon = daemon_mod.Daemon


class _Listener:
    def __init__(self, *, fails: bool = False, stop_after: Any = None) -> None:
        self.fails = fails
        self.stop_after = stop_after
        self.polls = 0
        self.opened = False
        self.closed = False
        self.reset = 0

    def reset_state(self) -> None:
        self.reset += 1

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


@pytest.mark.parametrize("event", ["press", "release", "panic"])
def test_state_changes_are_recorded_for_callers(event: str) -> None:
    # the self check reads these instead of guessing from the output level
    daemon = _daemon(_Listener())
    before = len(daemon.events(event))

    getattr(daemon, f"on_{event}")()

    assert len(daemon.events(event)) == before + 1


def test_events_ignore_what_happened_before_the_caller_looked() -> None:
    daemon = _daemon(_Listener())
    daemon.on_panic()
    marked = daemon.events("panic")[0]

    assert daemon.events("panic", since=marked - 1.0) == [marked]
    assert daemon.events("panic", since=marked + 1.0) == []
    assert daemon.events("release", since=0.0) == []


@pytest.mark.parametrize(
    "event,expected",
    [
        ("press", state_mod.OPEN),
        ("release", state_mod.CLOSING),
        ("panic", state_mod.PANIC),
    ],
)
def test_the_state_note_follows_the_microphone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, event: str, expected: str
) -> None:
    # `periferia status` and any future tray read this file. If it drifts from
    # what the microphone is doing, it will report a closed mic while it is
    # live, which is the one thing an indicator must never do.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    daemon = _daemon(_Listener())

    getattr(daemon, f"on_{event}")()
    note = state_mod.read()
    assert note is not None
    assert note["state"] == expected


def test_the_hold_running_out_is_what_closes_the_mic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # A release does not close the microphone, it schedules the close. The note
    # has to say so, or an indicator would read as open during the hold.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    daemon = _daemon(_Listener())

    daemon.cfg.audio.hold_ms = 50
    daemon.on_press()
    daemon.on_release()
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.CLOSING

    daemon._expire_hold()  # too early, the hold is still running
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.CLOSING

    daemon._release_at = 0.0
    daemon._expire_hold()
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.CLOSED


def _latched_daemon(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Daemon:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    daemon = _daemon(_Listener())
    daemon.cfg.ptt.latch_ms = 300
    daemon._pressed_at = 0.0
    return daemon


def test_latching_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    # Nobody asked for a latch they cannot see, so it has to be asked for.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    daemon = _daemon(_Listener())
    daemon._pressed_at = 0.0
    daemon.on_release()
    assert daemon._latched is False


def test_a_long_hold_latches_instead_of_closing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()

    assert daemon._latched
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.LATCHED
    # no release was scheduled, so nothing will close it behind the user's back
    assert daemon._release_at is None


def test_a_short_tap_closes_normally(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    # the threshold is 300 ms and this press lasted about none of it
    daemon._pressed_at = time.monotonic()
    daemon.on_release()

    assert daemon._latched is False
    assert daemon._release_at is not None


def test_the_next_press_closes_a_latched_mic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()
    assert daemon._latched

    daemon.on_press()
    assert daemon._latched is False
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.CLOSED


def test_releasing_a_latched_key_does_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # The key comes up after latching all the time. Treating that as a release
    # would close the mic a few milliseconds after the user asked to keep it.
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()
    daemon.on_release()
    daemon.on_release()

    assert daemon._latched
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.LATCHED


def test_max_press_does_not_cut_off_a_latched_mic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # The stuck-hold guard exists for a key that never comes up. A latched mic
    # has no key down at all, so that guard would close it for no reason.
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()
    assert daemon._latched

    daemon.cfg.ptt.max_press_ms = 1
    daemon._expire_stuck_hold()
    assert daemon._latched


def test_panic_still_closes_a_latched_mic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()
    assert daemon._latched

    daemon.on_panic()
    assert daemon._latched is False
    note = state_mod.read()
    assert note is not None and note["state"] == state_mod.PANIC


def test_forcing_a_release_also_unlatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # Otherwise the next press after any forced close would be swallowed as an
    # unlatch and the key would look dead until pressed twice.
    daemon = _latched_daemon(tmp_path, monkeypatch)
    daemon.on_press()
    daemon._pressed_at = 0.0
    daemon.on_release()
    assert daemon._latched

    daemon.force_release("test")
    assert daemon._latched is False


class TestEntryPoint:
    """`periferia-daemon -c` used to be ignored, because the entry point took
    no arguments at all. A config that was never read then looked like a config
    with wrong values in it."""

    def test_missing_named_config_stops_with_a_readable_error(
        self, tmp_path, monkeypatch, capsys
    ):
        from src.periferia.core import daemon as daemon_mod

        monkeypatch.setattr(
            daemon_mod, "Daemon", _should_not_start
        )
        monkeypatch.setattr(sys, "argv", ["periferia-daemon", "-c", str(tmp_path / "no.yaml")])
        assert daemon_mod.main() == 2
        assert "cannot read the config" in capsys.readouterr().err

    def test_it_reads_the_config_it_was_pointed_at(
        self, tmp_path, monkeypatch, caplog
    ):
        from src.periferia.core import daemon as daemon_mod

        cfg = tmp_path / "config.yaml"
        cfg.write_text("ptt:\n  ptt_key: KEY_GRAVE\n")
        seen: list[object] = []
        monkeypatch.setattr(daemon_mod, "Daemon", lambda cfg: seen.append(cfg) or _Stub())
        monkeypatch.setattr(sys, "argv", ["periferia-daemon", "-c", str(cfg)])
        assert daemon_mod.main() == 0
        assert seen[0].ptt.ptt_key == "KEY_GRAVE"

    def test_a_config_it_cannot_find_is_announced(
        self, tmp_path, monkeypatch, caplog
    ):
        from src.periferia.core import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "Daemon", lambda cfg: _Stub())
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        monkeypatch.setattr(sys, "argv", ["periferia-daemon"])
        with caplog.at_level(logging.WARNING):
            daemon_mod.main()
        assert any("no config found" in r.message for r in caplog.records)

    def test_the_config_in_force_is_named(
        self, tmp_path, monkeypatch, caplog
    ):
        from src.periferia.core import daemon as daemon_mod

        cfg = tmp_path / "config.yaml"
        cfg.write_text("ptt:\n  ptt_key: KEY_F12\n")
        monkeypatch.setattr(daemon_mod, "Daemon", lambda cfg: _Stub())
        monkeypatch.setattr(sys, "argv", ["periferia-daemon", "-c", str(cfg)])
        with caplog.at_level(logging.INFO):
            daemon_mod.main()
        assert any(str(cfg) in r.message for r in caplog.records)


class _Stub:
    """Just enough daemon for the entry point to install handlers and return."""

    def run(self) -> int:
        return 0

    def stop(self, *_: object) -> None:
        pass


def _should_not_start(cfg: object) -> _Stub:
    raise AssertionError("the daemon must not start on an unreadable config")


class TestWaitingForTheSoundServer:
    """The daemon used to lose the race against the session's PipeWire and die
    with a refused connection. With StartLimitBurst=5 that can leave the
    microphone dead for the whole session, so the wait is the fix."""

    def test_it_waits_and_then_carries_on(self, monkeypatch):
        from src.periferia.core import pipewire as pipewire_mod

        answers = iter([False, False, True])
        monkeypatch.setattr(pipewire_mod, "server_ready", lambda: next(answers))
        monkeypatch.setattr(pipewire_mod.time, "sleep", lambda s: None)
        assert pipewire_mod.wait_for_server(timeout=5, interval=0) is True

    def test_it_gives_up_after_the_timeout(self, monkeypatch):
        from src.periferia.core import pipewire as pipewire_mod

        monkeypatch.setattr(pipewire_mod, "server_ready", lambda: False)
        monkeypatch.setattr(pipewire_mod.time, "sleep", lambda s: None)
        assert pipewire_mod.wait_for_server(timeout=0, interval=0) is False

    def test_a_server_that_is_already_up_costs_nothing(self, monkeypatch):
        from src.periferia.core import pipewire as pipewire_mod

        calls = []
        monkeypatch.setattr(pipewire_mod, "server_ready", lambda: True)
        monkeypatch.setattr(pipewire_mod.time, "sleep", lambda s: calls.append(s))
        assert pipewire_mod.wait_for_server(timeout=30) is True
        assert calls == []

    def test_setup_says_a_server_is_missing_instead_of_crashing(self, monkeypatch):
        from src.periferia.core import pipewire as pipewire_mod

        monkeypatch.setattr(pipewire_mod, "wait_for_server", lambda *a, **k: False)
        assert _daemon_for(monkeypatch).setup() is False

    def test_a_refused_connection_is_a_message_not_a_traceback(self, monkeypatch):
        from src.periferia.core import pipewire as pipewire_mod

        monkeypatch.setattr(pipewire_mod, "wait_for_server", lambda *a, **k: True)

        def refuse(preferred: str) -> str:
            raise pipewire_mod.PipeWireError("Соединение отвергнуто")

        monkeypatch.setattr("periferia.modules.audio.pick_physical_source", refuse)
        assert _daemon_for(monkeypatch).setup() is False


def _daemon_for(monkeypatch):
    from src.periferia.core.daemon import Daemon

    return Daemon(config_mod.Config(audio=config_mod.AudioConfig(enabled=True)))


class TestRebuildRateLimit:
    """A held key repeats, and each repeat rebuilds the node applications are
    pointed at. One rebuild per burst, not one per press."""

    def _daemon(self):
        from src.periferia.core.daemon import Daemon

        return Daemon(config_mod.Config())

    def test_the_first_rebuild_is_allowed(self, monkeypatch):
        d = self._daemon()
        monkeypatch.setattr(d.mic, "pick_physical", lambda: "hw:0,0")
        started: list[str] = []
        monkeypatch.setattr(d.processing, "start", lambda p, name: started.append(name) or "new")
        monkeypatch.setattr(d.processing, "stop", lambda: None)
        d._rebuilt_at = 0.0
        assert d._rebuild_source() == "new"
        assert started == ["PeriferiaMic"]

    def test_a_second_one_within_the_cooldown_is_skipped(self, monkeypatch):
        d = self._daemon()
        monkeypatch.setattr(d.mic, "pick_physical", lambda: "hw:0,0")
        calls: list[str] = []
        monkeypatch.setattr(d.processing, "start", lambda p, name: calls.append(name) or "new")
        monkeypatch.setattr(d.processing, "stop", lambda: None)
        d._rebuilt_at = time.monotonic()
        assert d._rebuild_source() is None
        assert calls == []

    def test_it_works_again_once_the_cooldown_passes(self, monkeypatch):
        d = self._daemon()
        monkeypatch.setattr(d.mic, "pick_physical", lambda: "hw:0,0")
        monkeypatch.setattr(d.processing, "start", lambda p, name: "new")
        monkeypatch.setattr(d.processing, "stop", lambda: None)
        d._rebuilt_at = time.monotonic() - 60
        assert d._rebuild_source() == "new"

    def test_no_capture_device_means_no_rebuild(self, monkeypatch, caplog):
        d = self._daemon()
        monkeypatch.setattr(d.mic, "pick_physical", lambda: None)
        monkeypatch.setattr(d.processing, "start", lambda p, name: "should not happen")
        d._rebuilt_at = 0.0
        assert d._rebuild_source() is None
