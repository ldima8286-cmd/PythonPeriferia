from __future__ import annotations

import pytest

from periferia.modules import hotkey
from periferia.modules import audio as audio_mod

pytest.importorskip("evdev", reason="evdev needs access to /dev/input")


def test_russian_letters_map_to_physical_keys() -> None:
    # the whole point: evdev knows physical keys, not letters
    assert hotkey.RU_LETTERS["KEY_V"] == "М"
    assert hotkey.RU_LETTERS["KEY_N"] == "Т"
    assert hotkey.RU_LETTERS["KEY_GRAVE"] == "Ё"


def test_resolve_letter_and_code_agree() -> None:
    letter = hotkey.resolve_key("М")
    code = hotkey.resolve_key("KEY_V")
    assert letter == code
    assert letter is not None


def test_resolve_uppercase_letter() -> None:
    assert hotkey.resolve_key("М") == hotkey.resolve_key("м")
    assert hotkey.resolve_key("Т") == hotkey.resolve_key("KEY_N")


def test_resolve_unknown_returns_none() -> None:
    assert hotkey.resolve_key("KEY_DOES_NOT_EXIST") is None


def test_label_mentions_both() -> None:
    code = hotkey.resolve_key("KEY_H")
    assert code is not None
    label = hotkey.key_label(code)
    assert "Р" in label
    assert "H" in label


def test_repeat_events_do_not_trigger() -> None:
    got: list[str] = []
    listener = hotkey.HotkeyListener.__new__(hotkey.HotkeyListener)
    listener.ptt_code = 30
    listener.panic_code = None
    listener.ignore_repeat = True
    listener._down = False
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")
    listener.on_panic = None

    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.REPEAT)
    assert got == []

    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)
    assert got == ["press"]

    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE)
    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE)
    assert got == ["press", "release"]


def test_panic_bypasses_ptt() -> None:
    got: list[str] = []
    listener = hotkey.HotkeyListener.__new__(hotkey.HotkeyListener)
    listener.ptt_code = 30
    listener.panic_code = 88
    listener.ignore_repeat = True
    listener._down = True
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")
    listener.on_panic = lambda: got.append("panic")

    listener._handle(hotkey.ecodes.EV_KEY, 88, hotkey.PRESS)
    assert got == ["panic"]


def test_other_keys_are_ignored() -> None:
    got: list[str] = []
    listener = hotkey.HotkeyListener.__new__(hotkey.HotkeyListener)
    listener.ptt_code = 30
    listener.panic_code = None
    listener.ignore_repeat = True
    listener._down = False
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")
    listener.on_panic = None

    listener._handle(hotkey.ecodes.EV_KEY, 31, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_SYN, 0, 0)
    assert got == []


def test_ramp_curve_is_monotonic() -> None:
    for curve in ("exp", "linear", "s_curve"):
        values = [audio_mod._shape(i / 10, curve) for i in range(11)]
        assert values == sorted(values), curve
        assert values[0] == pytest.approx(0.0)
        assert values[-1] == pytest.approx(1.0)
