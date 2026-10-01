"""Macro playback, tested against a fake keyboard.

The properties worth testing are the ones that cost something when wrong: that a
stop lands between steps, that a key caught mid-hold is released, and that a
second trigger is refused instead of queued. None of them need a real uinput
device, and testing against a real one would not tell us what was written.
"""

from __future__ import annotations

import threading
import time

import pytest
from evdev import ecodes

from periferia.core.macro import Step
from periferia.modules import macrodevice

KEY_A = 30
KEY_B = 48


class FakeDevice:
    """Records what was written and how, so a test can read it back."""

    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []
        self.synced = 0
        self.closed = False
        self.lock = threading.Lock()

    def write(self, ev_type: int, code: int, value: int) -> None:
        with self.lock:
            self.events.append((ev_type, code, value))

    def syn(self) -> None:
        with self.lock:
            self.synced += 1

    def close(self) -> None:
        self.closed = True

    def keys(self) -> list[tuple[int, int]]:
        return [(code, value) for ev_type, code, value in self.events if ev_type == ecodes.EV_KEY]


@pytest.fixture
def player(monkeypatch: pytest.MonkeyPatch) -> macrodevice.MacroPlayer:
    """A player wired to a fake device and a sleep that records instead of waiting."""
    made: list[FakeDevice] = []

    def fake_input(**kwargs: object) -> FakeDevice:
        device = FakeDevice()
        made.append(device)
        return device

    monkeypatch.setattr(macrodevice, "UInput", fake_input)

    slept: list[float] = []

    def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    p = macrodevice.MacroPlayer(sleep=record_sleep)
    p.made = made  # type: ignore[attr-defined]
    p.slept = slept  # type: ignore[attr-defined]
    return p


def _keys(player: macrodevice.MacroPlayer) -> list[tuple[int, int]]:
    return player.made[0].keys()  # type: ignore[attr-defined]


def _wait(player: macrodevice.MacroPlayer) -> None:
    if player._thread is not None:
        player._thread.join(timeout=5.0)


class TestPlaying:
    def test_a_step_is_written_as_a_press_then_a_release(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)])
        _wait(player)
        assert _keys(player) == [(KEY_A, 1), (KEY_A, 0)]

    def test_a_gap_is_waited_before_the_press_not_after(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        """After the press, the gap is indistinguishable from a longer hold."""
        player.play([Step(code=KEY_A, gap_ms=200, hold_ms=40)])
        _wait(player)
        assert player.slept[0] == pytest.approx(0.2)  # type: ignore[attr-defined]


    def test_a_tiny_hold_is_still_held_long_enough_to_be_seen(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        """Shorter than the kernel repeat delay, some apps read it as stuck."""
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=1)])
        _wait(player)
        assert player.slept[0] >= macrodevice.MIN_HOLD_S  # type: ignore[attr-defined]

    def test_nothing_is_written_for_an_empty_macro(self, player: macrodevice.MacroPlayer) -> None:
        assert player.play([]) is False


class TestStopping:
    def test_a_stop_during_a_gap_stops_before_the_next_key_lands(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        def stop_during_sleep(seconds: float) -> None:
            player.stop()

        player._injected_sleep = stop_during_sleep
        steps = [Step(code=KEY_A, gap_ms=500, hold_ms=40), Step(code=KEY_B, gap_ms=0, hold_ms=40)]
        player.play(steps)
        _wait(player)
        assert _keys(player) == []

    def test_a_stop_mid_hold_still_lets_the_key_back_up(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        """A key is released even though nothing asked it to be.

        There is deliberately no stop check between the press and the release:
        abandoning a press half way is what leaves a key stuck down, and every
        window that saw it then believes a modifier is held.
        """
        player._injected_sleep = lambda seconds: player.stop()
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=2000)])
        _wait(player)
        assert _keys(player) == [(KEY_A, 1), (KEY_A, 0)]

    def test_a_device_failure_between_press_and_release_still_releases(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        """The one path that can skip the release: the device itself failing.

        Verified by breaking the code: removing the release in the cleanup block
        left every other test in this file passing, including the one named for
        it, because a stop alone always takes the ordinary path out.
        """
        device = FakeDevice()
        calls = {"n": 0}
        original = device.write

        def flaky(ev_type: int, code: int, value: int) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the release
                raise OSError("device went away")
            original(ev_type, code, value)

        device.write = flaky  # type: ignore[method-assign]
        player._ensure_device = lambda: device  # type: ignore[method-assign]
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)])
        _wait(player)
        # The failing call records nothing, so the press and the cleanup release
        # are the only two events there are to see.
        assert device.keys() == [(KEY_A, 1), (KEY_A, 0)]
        assert not player.playing

    def test_a_stop_raises_nothing_and_ends_the_thread(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        player._injected_sleep = lambda seconds: player.stop()
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)])
        _wait(player)
        assert player.playing is False


class TestNotQueueing:
    def test_a_second_macro_is_refused_while_one_is_playing(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        """A held or self-triggering key would otherwise build a backlog.

        The backlog keeps typing after the key that started it was released, which
        is the worst shape this bug can take.
        """
        release = threading.Event()
        player._injected_sleep = lambda seconds: release.wait(0.01)
        assert player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)]) is True
        assert player.play([Step(code=KEY_B, gap_ms=0, hold_ms=40)]) is False
        release.set()
        _wait(player)

    def test_playing_works_again_once_the_first_one_is_done(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)])
        _wait(player)
        assert player.play([Step(code=KEY_B, gap_ms=0, hold_ms=40)]) is True
        _wait(player)

    def test_a_finished_macro_leaves_the_player_usable(
        self, player: macrodevice.MacroPlayer
    ) -> None:
        player.play([Step(code=KEY_A, gap_ms=0, hold_ms=40)])
        _wait(player)
        assert player.playing is False


class TestRealTimeInterruption:
    """The tests above inject a sleeper, so they cannot catch the failure this
    class exists for: with a real sleeper the player has to notice stop() while
    it is waiting, not only once the wait is over on its own."""

    @pytest.fixture
    def real_player(self, monkeypatch: pytest.MonkeyPatch) -> macrodevice.MacroPlayer:
        made: list[FakeDevice] = []

        def fake_input(**kwargs: object) -> FakeDevice:
            device = FakeDevice()
            made.append(device)
            return device

        monkeypatch.setattr(macrodevice, "UInput", fake_input)
        player = macrodevice.MacroPlayer()  # no injected sleeper: waits for real
        player.made = made  # type: ignore[attr-defined]
        return player

    def _settle(self, player: macrodevice.MacroPlayer) -> None:
        deadline = time.monotonic() + 3.0
        while player.playing and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_a_five_second_gap_is_cut_short_by_a_stop(
        self, real_player: macrodevice.MacroPlayer
    ) -> None:
        steps = [
            Step(code=KEY_A, gap_ms=5000, hold_ms=40),
            Step(code=KEY_B, gap_ms=0, hold_ms=40),
        ]
        real_player.play(steps)
        time.sleep(0.05)  # let the thread get into the wait
        started = time.monotonic()
        real_player.stop()
        self._settle(real_player)
        elapsed = time.monotonic() - started
        real_player.close()
        assert not real_player.playing, "stop did not end the playback"
        assert elapsed < 1.0, f"waited {elapsed:.2f}s, so the gap was not interruptible"

    def test_no_keys_land_after_a_stop_mid_gap(
        self, real_player: macrodevice.MacroPlayer
    ) -> None:
        steps = [
            Step(code=KEY_A, gap_ms=5000, hold_ms=40),
            Step(code=KEY_B, gap_ms=0, hold_ms=40),
        ]
        real_player.play(steps)
        time.sleep(0.05)
        real_player.stop()
        self._settle(real_player)
        device: FakeDevice = real_player.made[0]  # type: ignore[attr-defined]
        real_player.close()
        assert device.keys() == []

    def test_a_key_held_at_the_moment_of_a_stop_is_released(
        self, real_player: macrodevice.MacroPlayer
    ) -> None:
        """Otherwise stopping leaves the key down in every window opened after,
        which is the kind of thing that eats a document."""
        real_player.play([Step(code=KEY_A, gap_ms=0, hold_ms=400)])
        time.sleep(0.08)  # pressed, and still inside the hold
        real_player.stop()
        self._settle(real_player)
        device: FakeDevice = real_player.made[0]  # type: ignore[attr-defined]
        real_player.close()
        keys = device.keys()
        assert (KEY_A, 0) in keys, f"key was never released: {keys}"
        assert keys[-1] == (KEY_A, 0)

    def test_a_short_macro_still_finishes_on_its_own(
        self, real_player: macrodevice.MacroPlayer
    ) -> None:
        real_player.play([Step(code=KEY_A, gap_ms=0, hold_ms=20)])
        self._settle(real_player)
        device: FakeDevice = real_player.made[0]  # type: ignore[attr-defined]
        real_player.close()
        assert device.keys() == [(KEY_A, 1), (KEY_A, 0)]

    def test_stop_is_safe_to_call_when_nothing_is_playing(
        self, real_player: macrodevice.MacroPlayer
    ) -> None:
        real_player.stop()
        real_player.close()
        assert not real_player.playing
