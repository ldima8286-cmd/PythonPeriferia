from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from periferia.modules import audio as audio_mod
from periferia.modules import hotkey

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
    listener._panic_down = False
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


class _FakeEvent:
    def __init__(self, type_: int, code: int, value: int) -> None:
        self.type = type_
        self.code = code
        self.value = value


class _FakeCaps:
    def __init__(self, caps: dict[int, list[int]]) -> None:
        self._caps = caps

    def get(self, kind: int, default: object = None) -> object:
        return self._caps.get(kind, default if default is not None else [])


class _FakeDev:
    """Stands in for evdev.InputDevice, backed by a real pipe."""

    def __init__(self, caps: dict[int, list[int]] | None = None) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.queued: list[_FakeEvent] = []
        self.calls: list[str] = []
        self.closed = False
        self._caps = _FakeCaps(caps or {})

    def capabilities(self) -> _FakeCaps:
        return self._caps

    @property
    def fd(self) -> int:
        return self.read_fd

    def read(self) -> list[_FakeEvent]:
        if self.queued:
            return self.queued
        os.read(self.read_fd, 1)
        return []

    def grab(self) -> None:
        self.calls.append("grab")

    def ungrab(self) -> None:
        self.calls.append("ungrab")

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        os.close(self.read_fd)
        os.close(self.write_fd)


def _listener(*devs: _FakeDev) -> hotkey.HotkeyListener:
    listener = hotkey.HotkeyListener.__new__(hotkey.HotkeyListener)
    listener._devs = {Path(f"/dev/input/event{i}"): d for i, d in enumerate(devs)}
    listener.devices = list(listener._devs)
    listener.ptt_code = 30
    listener.panic_code = None
    listener.ignore_repeat = True
    listener._down = False
    listener._panic_down = False
    listener.on_press = None
    listener.on_release = None
    listener.on_panic = None
    return listener


def test_poll_returns_on_timeout_without_events() -> None:
    # this is what lets the daemon expire hold_ms while the user types nothing
    dev = _FakeDev()
    started = time.monotonic()
    _listener(dev).poll(timeout=0.05)
    assert time.monotonic() - started < 1.0
    dev.close()


def test_poll_dispatches_queued_events() -> None:
    dev = _FakeDev()
    got: list[str] = []
    listener = _listener(dev)
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")

    dev.queued = [
        _FakeEvent(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS),
        _FakeEvent(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE),
    ]
    os.write(dev.write_fd, b"x")
    listener.poll(timeout=1.0)
    assert got == ["press", "release"]
    dev.close()


def test_duplicate_interfaces_of_one_keyboard_are_deduplicated() -> None:
    # a physical keyboard shows up as -event-kbd and -if02-event-kbd, and both
    # deliver the same presses. Watching both would double every event.
    assert hotkey.physical_device_id("usb-Company_Device-event-kbd") == "usb-Company_Device"
    assert (
        hotkey.physical_device_id("usb-Company_Device-if02-event-kbd") == "usb-Company_Device"
    )
    # a different physical device must not be folded in
    assert (
        hotkey.physical_device_id("usb-ITE_Tech._Inc._ITE_Device_8176_-event-kbd")
        != hotkey.physical_device_id("usb-Company_Device-event-kbd")
    )


def test_panic_fires_once_per_press() -> None:
    # the same key can arrive from two interfaces of one keyboard
    got: list[str] = []
    listener = _listener(_FakeDev())
    listener.panic_code = 88
    listener.on_panic = lambda: got.append("panic")

    listener._handle(hotkey.ecodes.EV_KEY, 88, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, 88, hotkey.PRESS)
    assert got == ["panic"]

    listener._handle(hotkey.ecodes.EV_KEY, 88, hotkey.RELEASE)
    listener._handle(hotkey.ecodes.EV_KEY, 88, hotkey.PRESS)
    assert got == ["panic", "panic"]


def test_duplicate_press_does_not_reopen_the_mic() -> None:
    # one physical press, two interfaces, one mic opening
    got: list[str] = []
    listener = _listener(_FakeDev())
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")

    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE)
    listener._handle(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE)
    assert got == ["press", "release"]


def test_mouse_is_recognised_for_side_button_ptt() -> None:
    mouse = _FakeDev(
        {
            hotkey.ecodes.EV_KEY: [hotkey.ecodes.BTN_LEFT, hotkey.ecodes.BTN_SIDE],
            hotkey.ecodes.EV_REL: [hotkey.ecodes.REL_X, hotkey.ecodes.REL_Y],
        }
    )
    assert hotkey.is_pointer(mouse) is True
    # it is a mouse, not a keyboard, so it must not pass the keyboard test
    assert hotkey.is_keyboard(mouse) is False

    keyboard = _FakeDev(
        {
            hotkey.ecodes.EV_KEY: [
                hotkey.ecodes.KEY_ENTER,
                hotkey.ecodes.KEY_SPACE,
                hotkey.ecodes.KEY_GRAVE,
            ],
        }
    )
    assert hotkey.is_keyboard(keyboard) is True
    assert hotkey.is_pointer(keyboard) is False


def test_side_button_can_be_the_ptt_key() -> None:
    # BTN_SIDE is a mouse button, and the design calls for using it as PTT
    got: list[str] = []
    listener = _listener(_FakeDev())
    listener.ptt_code = hotkey.ecodes.BTN_SIDE
    listener.on_press = lambda: got.append("press")
    listener.on_release = lambda: got.append("release")

    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.BTN_SIDE, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.BTN_SIDE, hotkey.RELEASE)
    assert got == ["press", "release"]


def test_mouse_motion_does_not_trigger_ptt() -> None:
    # a mouse is watched, but only its buttons may matter
    got: list[str] = []
    listener = _listener(_FakeDev())
    listener.ptt_code = hotkey.ecodes.BTN_SIDE
    listener.on_press = lambda: got.append("press")

    listener._handle(hotkey.ecodes.EV_REL, hotkey.ecodes.REL_X, 5)
    listener._handle(hotkey.ecodes.EV_REL, hotkey.ecodes.REL_WHEEL, -1)
    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.BTN_LEFT, hotkey.PRESS)
    assert got == []


def test_mouse_is_only_watched_when_the_key_is_a_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # reading a mouse needs an extra udev rule, so it must not be opened
    # unless the PTT key is actually one of its buttons
    mouse = _FakeDev(
        {
            hotkey.ecodes.EV_KEY: [hotkey.ecodes.BTN_LEFT, hotkey.ecodes.BTN_SIDE],
            hotkey.ecodes.EV_REL: [hotkey.ecodes.REL_X],
        }
    )
    kbd = _FakeDev(
        {hotkey.ecodes.EV_KEY: [hotkey.ecodes.KEY_ENTER, hotkey.ecodes.KEY_SPACE]}
    )
    links = [
        (Path("/dev/input/event4"), "usb-Vendor_Kbd-event-kbd"),
        (Path("/dev/input/event9"), "usb-Vendor_Mouse-if01-event-kbd"),
    ]
    monkeypatch.setattr(hotkey, "list_input_devices", lambda: links)
    monkeypatch.setattr(
        hotkey, "open_device", lambda path: mouse if "event9" in str(path) else kbd
    )

    plain = hotkey.find_keyboards("auto")
    assert [str(p) for p in plain] == ["/dev/input/event4"]

    with_mouse = hotkey.find_keyboards("auto", include_pointers=True)
    assert [str(p) for p in with_mouse] == ["/dev/input/event4", "/dev/input/event9"]


def test_is_button_code_tells_keys_from_mouse_buttons() -> None:
    assert hotkey.is_button_code(hotkey.resolve_key("BTN_SIDE") or 0) is True
    assert hotkey.is_button_code(hotkey.resolve_key("KEY_GRAVE") or 0) is False
    assert hotkey.is_button_code(hotkey.resolve_key("KEY_F12") or 0) is False


def test_open_does_not_grab_the_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    # grabbing would swallow every keystroke system wide
    dev = _FakeDev()
    monkeypatch.setattr(hotkey, "open_device", lambda path: dev)

    listener = hotkey.HotkeyListener(Path("/dev/input/event5"), 30)
    listener.open()

    assert "grab" not in dev.calls
    assert "ungrab" not in dev.calls

    listener.close()
    assert "close" in dev.calls


def test_press_on_one_keyboard_releases_on_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a laptop keyboard and a USB one are two devices, but they are one user
    # intention: holding PTT on one and releasing on the other must not stick
    got: list[str] = []
    laptop, usb = _FakeDev(), _FakeDev()
    monkeypatch.setattr(
        hotkey, "open_device", lambda path: (laptop if "event4" in str(path) else usb)
    )

    listener = hotkey.HotkeyListener(
        [Path("/dev/input/event4"), Path("/dev/input/event5")],
        30,
        on_press=lambda: got.append("press"),
        on_release=lambda: got.append("release"),
    )
    listener.open()

    laptop.queued = [_FakeEvent(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)]
    os.write(laptop.write_fd, b"x")
    listener.poll(timeout=1.0)

    usb.queued = [_FakeEvent(hotkey.ecodes.EV_KEY, 30, hotkey.RELEASE)]
    os.write(usb.write_fd, b"x")
    listener.poll(timeout=1.0)

    assert got == ["press", "release"]
    laptop.close()
    usb.close()


def test_unplugged_keyboard_does_not_kill_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pulling out a USB keyboard mid hold is normal, not a reason to lose PTT
    got: list[str] = []
    dying, alive = _FakeDev(), _FakeDev()
    monkeypatch.setattr(
        hotkey, "open_device", lambda path: (dying if "event4" in str(path) else alive)
    )

    listener = hotkey.HotkeyListener(
        [Path("/dev/input/event4"), Path("/dev/input/event5")],
        30,
        on_press=lambda: got.append("press"),
        on_release=lambda: got.append("release"),
    )
    listener.open()

    os.close(dying.read_fd)
    dying.closed = True

    alive.queued = [_FakeEvent(hotkey.ecodes.EV_KEY, 30, hotkey.PRESS)]
    os.write(alive.write_fd, b"x")
    listener.poll(timeout=1.0)

    assert Path("/dev/input/event4") not in listener._devs
    assert Path("/dev/input/event5") in listener._devs
    assert got == ["press"]
    alive.close()
    listener.close()


class _FakeDevWithPhys(_FakeDev):
    def __init__(self, caps: dict[int, list[int]], phys: str = "") -> None:
        super().__init__(caps)
        self.phys = phys


def test_a_keyboard_without_a_by_id_link_is_still_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A USB keyboard with no serial gets no by-id symlink at all. Reading only
    # by-id dropped it, so the daemon watched the laptop keyboard and the
    # external one looked broken with nothing in the log to say why.
    links = [
        (Path("/dev/input/event4"), "usb-Vendor_Kbd-event-kbd"),
        (Path("/dev/input/event7"), "event7"),
    ]
    caps = {
        hotkey.ecodes.EV_KEY: [
            hotkey.ecodes.KEY_GRAVE,
            hotkey.ecodes.KEY_ENTER,
            hotkey.ecodes.KEY_SPACE,
        ]
    }
    monkeypatch.setattr(hotkey, "list_input_devices", lambda: links)
    monkeypatch.setattr(hotkey, "open_device", lambda p: _FakeDevWithPhys(caps, ""))

    assert [str(p) for p in hotkey.find_keyboards("auto")] == [
        "/dev/input/event4",
        "/dev/input/event7",
    ]


def test_every_interface_of_one_keyboard_is_watched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real external keyboard here exposed three nodes on one physical
    # device. All three advertised typing keys, two stayed silent, and only
    # the third delivered presses. Keeping one node per device made PTT dead
    # with nothing logged, so every usable node has to be watched.
    caps = {
        hotkey.ecodes.EV_KEY: [
            hotkey.ecodes.KEY_GRAVE,
            hotkey.ecodes.KEY_ENTER,
            hotkey.ecodes.KEY_SPACE,
        ]
    }
    links = [
        (Path("/dev/input/event4"), "usb-COMPANY_USB_Device-event-kbd"),
        (Path("/dev/input/event5"), "usb-COMPANY_USB_Device-event-if01"),
        (Path("/dev/input/event6"), "usb-COMPANY_USB_Device-if02-event-kbd"),
    ]
    monkeypatch.setattr(hotkey, "list_input_devices", lambda: links)
    monkeypatch.setattr(
        hotkey,
        "open_device",
        lambda p: _FakeDevWithPhys(caps, f"usb-0000:00:14.0-3/input{p.name[-1]}"),
    )

    assert [str(p) for p in hotkey.find_keyboards("auto")] == [
        "/dev/input/event4",
        "/dev/input/event5",
        "/dev/input/event6",
    ]


def test_a_duplicate_press_does_not_fire_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    # two interfaces of one keyboard both report the same press, which is
    # exactly why every node can be watched safely
    dev = _FakeDev()
    monkeypatch.setattr(hotkey, "open_device", lambda p: dev)
    presses: list[int] = []
    releases: list[int] = []
    listener = hotkey.HotkeyListener(
        Path("/dev/input/event4"),
        hotkey.ecodes.KEY_GRAVE,
        on_press=lambda: presses.append(1),
        on_release=lambda: releases.append(1),
    )

    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.KEY_GRAVE, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.KEY_GRAVE, hotkey.PRESS)
    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.KEY_GRAVE, hotkey.RELEASE)
    listener._handle(hotkey.ecodes.EV_KEY, hotkey.ecodes.KEY_GRAVE, hotkey.RELEASE)

    assert len(presses) == 1
    assert len(releases) == 1


def test_two_different_keyboards_are_both_watched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the laptop and an external one must not collapse into a single device
    kbd = {
        hotkey.ecodes.EV_KEY: [
            hotkey.ecodes.KEY_GRAVE,
            hotkey.ecodes.KEY_ENTER,
            hotkey.ecodes.KEY_SPACE,
        ]
    }
    phys = {
        "event4": "usb-0000:00:14.0-2/input0",
        "event7": "isa0060:00/1/input0",
    }
    links = [
        (Path("/dev/input/event4"), "usb-Vendor_Kbd-event-kbd"),
        (Path("/dev/input/event7"), "i2c-I2C-HID-event-kbd"),
    ]
    monkeypatch.setattr(hotkey, "list_input_devices", lambda: links)
    monkeypatch.setattr(
        hotkey, "open_device", lambda p: _FakeDevWithPhys(kbd, phys[p.name])
    )

    assert [str(p) for p in hotkey.find_keyboards("auto")] == [
        "/dev/input/event4",
        "/dev/input/event7",
    ]


def test_device_group_falls_back_to_the_name_without_phys() -> None:
    # an old kernel or a driver that reports no phys must still group sensibly
    dev = _FakeDevWithPhys({}, "")
    assert hotkey.device_group(dev, "usb-Vendor_Kbd-event-kbd", Path("/dev/input/event4")) == (
        "usb-Vendor_Kbd"
    )
    assert hotkey.device_group(dev, "event4", Path("/dev/input/event4")) == "event4"
