"""Macro triggers and the recording path in HotkeyListener.

Two things are tested here that the PTT tests never touched:

  - a key bound to a macro plays it once, not once per event, even when the
    same keypress arrives twice from two interfaces of one keyboard;
  - while a recorder is attached, nothing else happens. A recording that also
    opened the microphone or fired a macro on its first keypress would be
    unusable, and the failure would look like a broken recorder rather than a
    broken listener.

Panic has to keep working throughout, because it is the way out of a recording
that needs stopping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evdev import ecodes

from periferia.modules import hotkey

PRESS = hotkey.PRESS
RELEASE = hotkey.RELEASE
REPEAT = hotkey.REPEAT


class _FakeDev:
    def __init__(self, events: list[tuple[int, int, int]]) -> None:
        self._events = events
        self.fd = 1
        self._reads = 0

    def read(self) -> list[Any]:
        self._reads += 1
        events = self._events
        self._events = []
        return [_Ev(*e) for e in events]


class _Ev:
    def __init__(self, type: int, code: int, value: int) -> None:
        self.type = type
        self.code = code
        self.value = value


def _listener(**kwargs: Any) -> hotkey.HotkeyListener:
    listener = hotkey.HotkeyListener.__new__(hotkey.HotkeyListener)
    listener.devices = []
    listener.ptt_code = 30
    listener.panic_code = None
    listener.ignore_repeat = True
    listener.on_press = None
    listener.on_release = None
    listener.on_panic = None
    listener.macro_codes = {}
    listener.on_any_key = None
    listener._devs = {}
    listener._down = False
    listener._panic_down = False
    listener._macro_down = set()
    for name, value in kwargs.items():
        setattr(listener, name, value)
    return listener


class TestMacroTriggers:
    def test_pressing_a_macro_key_plays_the_macro(self) -> None:
        played: list[int] = []
        listener = _listener(macro_codes={63: lambda: played.append(63)})
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        assert played == [63]

    def test_releasing_does_not_play_it_again(self) -> None:
        played: list[int] = []
        listener = _listener(macro_codes={63: lambda: played.append(63)})
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        listener._handle(ecodes.EV_KEY, 63, RELEASE)
        assert played == [63]

    def test_the_same_key_from_two_interfaces_plays_it_once(self) -> None:
        """A keyboard can report one physical keypress through two event nodes.
        Playing the macro twice would double every character it types."""
        played: list[int] = []
        listener = _listener(macro_codes={63: lambda: played.append(63)})
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        assert played == [63]

    def test_autorepeat_does_not_replay_a_held_macro_key(self) -> None:
        played: list[int] = []
        listener = _listener(macro_codes={63: lambda: played.append(63)})
        for _ in range(5):
            listener._handle(ecodes.EV_KEY, 63, REPEAT)
        assert played == []

    def test_pressing_again_after_releasing_plays_again(self) -> None:
        played: list[int] = []
        listener = _listener(macro_codes={63: lambda: played.append(63)})
        for _ in range(3):
            listener._handle(ecodes.EV_KEY, 63, PRESS)
            listener._handle(ecodes.EV_KEY, 63, RELEASE)
        assert played == [63, 63, 63]

    def test_a_macro_key_leaves_the_microphone_alone(self) -> None:
        """A macro key next to PTT must not drag the mic open with it."""
        pressed: list[int] = []
        listener = _listener(
            macro_codes={63: lambda: pressed.append(1)},
            on_press=lambda: pressed.append(2),
        )
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        assert pressed == [1, 2]

    def test_ptt_wins_when_a_macro_is_bound_to_the_ptt_key(self) -> None:
        """The validator rejects this, but a config that somehow contains it
        must not leave the user with a dead push-to-talk. It degrades to PTT."""
        pressed: list[int] = []
        listener = _listener(
            macro_codes={30: lambda: pressed.append(1)},
            on_press=lambda: pressed.append(2),
        )
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        assert pressed == [2]

    def test_two_macros_on_two_keys_both_fire(self) -> None:
        played: list[int] = []
        listener = _listener(
            macro_codes={63: lambda: played.append(63), 64: lambda: played.append(64)}
        )
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        listener._handle(ecodes.EV_KEY, 64, PRESS)
        assert played == [63, 64]


class TestRecordingTakesTheStream:
    def test_every_key_reaches_the_recorder(self) -> None:
        seen: list[tuple[int, int]] = []
        listener = _listener(on_any_key=lambda t, c, v: seen.append((c, v)))
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        listener._handle(ecodes.EV_KEY, 30, RELEASE)
        assert seen == [(30, PRESS), (31, PRESS), (30, RELEASE)]

    def test_recording_does_not_open_the_microphone(self) -> None:
        pressed: list[int] = []
        listener = _listener(
            on_any_key=lambda t, c, v: None,
            on_press=lambda: pressed.append(1),
        )
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        assert pressed == []

    def test_recording_does_not_fire_macros(self) -> None:
        played: list[int] = []
        listener = _listener(
            on_any_key=lambda t, c, v: None,
            macro_codes={30: lambda: played.append(30)},
        )
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        assert played == []

    def test_panic_still_works_while_recording(self) -> None:
        """Otherwise there is no way to end a recording that went wrong."""
        panicked: list[int] = []
        listener = _listener(
            panic_code=31,
            on_any_key=lambda t, c, v: None,
            on_panic=lambda: panicked.append(1),
        )
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        assert panicked == [1]

    def test_the_panic_key_is_never_recorded(self) -> None:
        """Panic ends a recording rather than becoming part of one. Letting it
        through would put the one key that stops everything into a macro."""
        seen: list[int] = []
        listener = _listener(
            panic_code=31, on_any_key=lambda t, c, v: seen.append(c)
        )
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        assert seen == []

    def test_the_recorder_is_told_when_the_key_was_pressed(self) -> None:
        times: list[float] = []
        listener = _listener(on_any_key=lambda t, c, v: times.append(t))
        listener._handle(ecodes.EV_KEY, 30, PRESS)
        assert len(times) == 1 and times[0] > 0

    def test_recording_stops_using_the_mic_when_detached(self) -> None:
        """The recorder hands the stream back to the normal dispatch when it is
        finished, so PTT and macros go back to working."""
        played: list[int] = []
        listener = _listener(
            on_any_key=lambda t, c, v: None,
            macro_codes={63: lambda: played.append(63)},
        )
        listener.on_any_key = None
        listener._handle(ecodes.EV_KEY, 63, PRESS)
        assert played == [63]


class TestPanicUnchanged:
    def test_panic_is_reported_once_per_press(self) -> None:
        panicked: list[int] = []
        listener = _listener(panic_code=31, on_panic=lambda: panicked.append(1))
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        assert panicked == [1]

    def test_panic_does_not_also_trigger_a_macro_on_the_same_key(self) -> None:
        played: list[int] = []
        listener = _listener(
            panic_code=31,
            on_panic=lambda: None,
            macro_codes={31: lambda: played.append(31)},
        )
        listener._handle(ecodes.EV_KEY, 31, PRESS)
        assert played == []


class TestMacroCodesFromConfig:
    def test_the_argument_accepts_a_mapping_and_is_copied(self) -> None:
        """A copy, so a caller mutating its dict afterwards cannot make the
        listener fire something it was never configured for."""

        class _Devices:
            def __iter__(self) -> Any:
                return iter(())

        codes = {63: lambda: None}
        listener = hotkey.HotkeyListener(Path("/dev/input/event0"), 30, macro_codes=codes)
        assert listener.macro_codes == codes
        codes.clear()
        assert listener.macro_codes == {63: listener.macro_codes[63]}
