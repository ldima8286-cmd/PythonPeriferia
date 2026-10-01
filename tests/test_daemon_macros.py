"""The daemon's side of macros: loading them, firing them, and panic.

The pieces are tested elsewhere, so what matters here is the wiring and the
promises the daemon makes about a macro:

  - a macro key plays the macro the config says;
  - a second press replaces the first instead of queueing behind it;
  - panic stops a macro mid-flight, which is the whole reason it exists.

These run against a fake player, because a real one needs /dev/uinput.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from periferia.core import config as config_mod
from periferia.core.daemon import Daemon
from periferia.core.macro import Step

KEY_F5 = 63
KEY_F6 = 64
KEY_H = 35
KEY_I = 23


class FakePlayer:
    """Stands in for MacroPlayer and remembers what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Step, ...], bool]] = []
        self.stopped = 0
        self.closed = 0
        self.playing = False

    def play(self, steps: Any, *, replace: bool = False) -> bool:
        self.calls.append((tuple(steps), replace))
        self.playing = True
        return True

    def stop(self) -> None:
        self.stopped += 1
        self.playing = False

    def close(self) -> None:
        # Mirrors MacroPlayer: closing stops first, so a key still held is let
        # go. A fake that skipped this would let a player that forgot pass.
        self.stop()
        self.closed += 1


class RefusingPlayer(FakePlayer):
    """A player with nowhere to play to, like a machine without /dev/uinput."""

    def play(self, steps: Any, *, replace: bool = False) -> bool:
        return False


class _Stub:
    def __init__(self, **_: Any) -> None:
        self.source = "stub"

    def panic(self) -> None:
        self.panicked = True

    def force_silence(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def close_mic(self) -> None:
        pass

    def rebuild_source(self) -> str | None:
        return None


def _step(key: str, gap_ms: int = 0, hold_ms: int = 40) -> config_mod.MacroStep:
    return config_mod.MacroStep(key=key, gap_ms=gap_ms, hold_ms=hold_ms)


def _make(
    macros: list[config_mod.MacroConfig] | None = None,
    profiles: list[config_mod.ProfileConfig] | None = None,
    player: FakePlayer | None = None,
) -> Daemon:
    """A daemon with no audio and a fake macro player.

    Built without __init__ on purpose: starting the real thing would need
    PipeWire, which is not available where these run.
    """
    d = Daemon.__new__(Daemon)
    d.cfg = config_mod.Config(
        ptt=config_mod.PttConfig(ptt_key="KEY_GRAVE", panic_key="KEY_F12"),
        macros=macros or [],
        profiles=profiles or [],
    )
    d.mic = _Stub()  # type: ignore[assignment]
    d.macros = player or FakePlayer()  # type: ignore[assignment]
    d._macros_by_name = {}
    d._macro_codes = {}
    d._latched = False
    d._release_at = None
    d._pressed_at = None
    d._rebuilt_at = 0.0
    d._stop = threading.Event()
    d._events = []
    d._events_lock = threading.Lock()
    d.state_mod = _Stub()  # type: ignore[attr-defined]
    return d


@pytest.fixture
def daemon() -> Daemon:
    return _make(
        macros=[
            config_mod.MacroConfig(
                name="hi",
                bind="KEY_F5",
                steps=[_step("KEY_H"), _step("KEY_I", gap_ms=50)],
            )
        ]
    )


class TestLoadingMacros:
    def test_a_bound_macro_is_found(self, daemon: Daemon) -> None:
        assert daemon.load_macros() == 1
        assert "hi" in daemon._macros_by_name

    def test_the_trigger_key_is_resolved_to_a_code(self, daemon: Daemon) -> None:
        daemon.load_macros()
        assert daemon._macro_codes == {KEY_F5: "hi"}

    def test_the_trigger_maps_to_something_callable(self, daemon: Daemon) -> None:
        daemon.load_macros()
        assert set(daemon._macro_triggers()) == {KEY_F5}

    def test_an_unbound_macro_is_kept_but_not_playable(self) -> None:
        """It can still be played by name; there is just no key to press."""
        d = _make(macros=[config_mod.MacroConfig(name="m", bind="", steps=[_step("KEY_H")])])
        assert d.load_macros() == 0
        assert "m" in d._macros_by_name

    def test_a_macro_with_an_unknown_key_is_skipped_not_fatal(self) -> None:
        """The daemon has to start anyway: a typo in one macro must not take
        the microphone down with it."""
        d = _make(
            macros=[
                config_mod.MacroConfig(name="good", bind="KEY_F5", steps=[_step("KEY_H")]),
                config_mod.MacroConfig(
                    name="bad", bind="KEY_MUHLE_FN", steps=[_step("KEY_H")]
                ),
            ]
        )
        assert d.load_macros() == 1
        assert d._macro_codes == {KEY_F5: "good"}

    def test_no_macros_at_all_is_fine(self) -> None:
        d = _make()
        assert d.load_macros() == 0
        assert d._macro_triggers() == {}

    def test_the_profile_copy_wins_over_the_global_one(self) -> None:
        """The reason for per-profile macros exists."""
        d = _make(
            macros=[config_mod.MacroConfig(name="m", bind="KEY_F5", steps=[_step("KEY_H")])],
            profiles=[
                config_mod.ProfileConfig(
                    name="game",
                    enabled=True,
                    macros=[
                        config_mod.MacroConfig(
                            name="m", bind="KEY_F6", steps=[_step("KEY_I")]
                        )
                    ],
                )
            ],
        )
        d.load_macros()
        assert d._macro_codes == {KEY_F6: "m"}
        assert [s.code for s in d._macros_by_name["m"].steps] == [KEY_I]

    def test_a_disabled_profile_does_not_override(self) -> None:
        d = _make(
            macros=[config_mod.MacroConfig(name="m", bind="KEY_F5", steps=[_step("KEY_H")])],
            profiles=[
                config_mod.ProfileConfig(
                    name="off",
                    enabled=False,
                    macros=[
                        config_mod.MacroConfig(name="m", bind="KEY_F6", steps=[_step("KEY_I")])
                    ],
                )
            ],
        )
        d.load_macros()
        assert d._macro_codes == {KEY_F5: "m"}

    def test_two_macros_on_one_key_keep_the_last_one(self) -> None:
        """The validator reports this; the daemon still has to pick something
        rather than guess per keypress."""
        d = _make(
            macros=[
                config_mod.MacroConfig(name="one", bind="KEY_F5", steps=[_step("KEY_H")]),
                config_mod.MacroConfig(name="two", bind="KEY_F5", steps=[_step("KEY_I")]),
            ]
        )
        d.load_macros()
        assert d._macro_codes == {KEY_F5: "two"}


class TestPlayingAMacro:
    def test_pressing_the_key_plays_the_recorded_keys(self, daemon: Daemon) -> None:
        daemon.load_macros()
        daemon._macro_triggers()[KEY_F5]()
        steps, _ = daemon.macros.calls[0]  # type: ignore[attr-defined]
        assert [s.code for s in steps] == [KEY_H, KEY_I]

    def test_the_gaps_from_the_recording_are_kept(self, daemon: Daemon) -> None:
        """A macro that lost its timing would type the right letters at the
        wrong speed, which is not the same macro."""
        daemon.load_macros()
        daemon._macro_triggers()[KEY_F5]()
        steps, _ = daemon.macros.calls[0]  # type: ignore[attr-defined]
        assert steps[1].gap_ms == 50

    def test_a_second_press_replaces_the_first(self, daemon: Daemon) -> None:
        """No queue: five presses of a macro key should not type five macros."""
        daemon.load_macros()
        trigger = daemon._macro_triggers()[KEY_F5]
        trigger()
        trigger()
        assert len(daemon.macros.calls) == 2  # type: ignore[attr-defined]
        assert all(replace for _, replace in daemon.macros.calls)  # type: ignore[attr-defined]

    def test_the_play_is_recorded_as_an_event(self, daemon: Daemon) -> None:
        """So the GUI and the TUI can show that a macro actually fired."""
        daemon.load_macros()
        daemon._macro_triggers()[KEY_F5]()
        assert len(daemon.events("macro:hi")) == 1

    def test_playing_an_unknown_name_does_nothing(self, daemon: Daemon) -> None:
        daemon.load_macros()
        daemon.play_macro("nosuch")
        assert daemon.macros.calls == []  # type: ignore[attr-defined]

    def test_playing_before_loading_does_nothing(self, daemon: Daemon) -> None:
        daemon.play_macro("hi")
        assert daemon.macros.calls == []  # type: ignore[attr-defined]

    def test_nothing_is_recorded_when_the_macro_could_not_start(self) -> None:
        """Otherwise the GUI would claim a macro ran when it did not."""
        d = _make(
            macros=[
                config_mod.MacroConfig(name="hi", bind="KEY_F5", steps=[_step("KEY_H")])
            ],
            player=RefusingPlayer(),
        )
        d.load_macros()
        d._macro_triggers()[KEY_F5]()
        assert d.events("macro:hi") == []


class TestPanicStopsAMacro:
    def test_panic_stops_a_playing_macro(self, daemon: Daemon) -> None:
        daemon.load_macros()
        daemon.macros.playing = True  # type: ignore[attr-defined]
        daemon.on_panic()
        assert daemon.macros.stopped == 1  # type: ignore[attr-defined]
        assert daemon.mic.panicked is True  # type: ignore[attr-defined]

    def test_panic_stops_the_macro_before_it_closes_the_mic(self) -> None:
        """A macro that replays its own trigger would otherwise keep typing
        after the microphone was already shut."""
        order: list[str] = []

        class _Ordered(FakePlayer):
            def stop(self) -> None:
                order.append("macro")

        d = _make(
            macros=[
                config_mod.MacroConfig(name="hi", bind="KEY_F5", steps=[_step("KEY_H")])
            ],
            player=_Ordered(),
        )
        d.mic.panic = lambda: order.append("mic")  # type: ignore[method-assign,attr-defined]
        d.macros.playing = True  # type: ignore[attr-defined]
        d.on_panic()
        assert order == ["macro", "mic"]

    def test_panic_drops_the_latch_too(self, daemon: Daemon) -> None:
        """Left set, the next press reads as an unlatch and the mic never opens
        again."""
        daemon._latched = True
        daemon.on_panic()
        assert daemon._latched is False

    def test_panic_stops_a_macro_even_while_its_key_is_held(self, daemon: Daemon) -> None:
        daemon.load_macros()
        daemon.macros.playing = True  # type: ignore[attr-defined]
        daemon.on_panic()
        assert daemon.macros.stopped == 1  # type: ignore[attr-defined]

    def test_panic_with_nothing_playing_is_harmless(self, daemon: Daemon) -> None:
        daemon.load_macros()
        daemon.on_panic()
        assert daemon.mic.panicked is True  # type: ignore[attr-defined]


class _StubListener:
    """A listener that ends the loop on its first poll."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def poll(self, timeout: float = 0.2) -> None:
        self._stop.set()


class _StubProcessing:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class TestShutdownReleasesKeys:
    """The exit path has to let go of anything a macro was holding.

    Without it, a macro interrupted by Ctrl+C leaves a key down for the rest of
    the session, and nothing in the log says so.
    """

    def _run_once(self, daemon: Daemon) -> _StubListener:
        listener = _StubListener(daemon._stop)
        daemon.setup = lambda: True  # type: ignore[method-assign]
        daemon._listener = listener  # type: ignore[assignment]
        daemon.processing = _StubProcessing()  # type: ignore[attr-defined]
        daemon.load_macros = lambda: 0  # type: ignore[method-assign]
        daemon.macros.playing = True  # type: ignore[attr-defined]
        assert daemon.run() == 0
        return listener

    def test_the_macro_player_is_closed_on_the_way_out(self, daemon: Daemon) -> None:
        self._run_once(daemon)
        assert daemon.macros.closed == 1  # type: ignore[attr-defined]

    def test_the_macro_is_stopped_so_no_key_stays_held(self, daemon: Daemon) -> None:
        self._run_once(daemon)
        assert daemon.macros.stopped >= 1  # type: ignore[attr-defined]
        assert daemon.macros.playing is False  # type: ignore[attr-defined]

    def test_the_listener_is_still_closed_too(self, daemon: Daemon) -> None:
        assert self._run_once(daemon).closed is True

    def test_the_microphone_is_silenced_and_released(self, daemon: Daemon) -> None:
        self._run_once(daemon)
        assert daemon.processing.stopped == 1  # type: ignore[attr-defined]
