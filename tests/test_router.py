"""Tests for the router's decision making, which needs no hardware.

The ioctl half cannot run here. What can be pinned down is which code ends up
where, because getting that backwards silently repoints somebody's push-to-talk
at a different key.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from evdev import ecodes

from src.periferia.modules.hotkey import VIRTUAL_NAME_PREFIX, is_virtual_device
from src.periferia.modules.remap import build_remap
from src.periferia.modules.router import KeyAction, _Channel, clone_capabilities, plan


def code(name: str) -> int:
    return ecodes.ecodes[name]


CAPS = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
PTT = code("KEY_GRAVE")
PANIC = code("KEY_F12")
WATCH = frozenset({PTT, PANIC})


class TestVirtualIdentity:
    def test_our_own_device_is_recognised(self):
        assert is_virtual_device(f"{VIRTUAL_NAME_PREFIX} keyboard event6")

    def test_real_device_is_not_ours(self):
        assert not is_virtual_device("E-Signal KB-120 Kailh Box")
        assert not is_virtual_device("AT Translated Set 2 keyboard")

    def test_prefix_match_is_exact_at_the_start(self):
        assert not is_virtual_device(f"Something {VIRTUAL_NAME_PREFIX} keyboard")


class TestCloneCapabilities:
    def test_saves_led_and_rep(self):
        caps = {
            ecodes.EV_KEY: [code("KEY_A")],
            ecodes.EV_LED: [code("LED_NUML")],
            ecodes.EV_REP: [],
        }
        assert clone_capabilities(caps)[ecodes.EV_LED] == [code("LED_NUML")]

    def test_drops_syntax_and_ff(self):
        caps = {
            ecodes.EV_KEY: [code("KEY_A")],
            ecodes.EV_SYN: [0, 0, 0],
            ecodes.EV_FF: [ecodes.FF_RUMBLE],
        }
        out = clone_capabilities(caps)
        assert ecodes.EV_SYN not in out
        assert ecodes.EV_FF not in out

    def test_drops_empty_types(self):
        assert clone_capabilities({ecodes.EV_KEY: [1], ecodes.EV_REL: []}) == {ecodes.EV_KEY: [1]}


class TestPlan:
    def test_desktop_gets_the_remap(self):
        forwarded, _ = plan([(ecodes.EV_KEY, code("KEY_CAPSLOCK"), 1)], CAPS, WATCH)
        assert forwarded == [(ecodes.EV_KEY, code("KEY_ESC"), 1)]

    def test_application_gets_the_physical_key(self):
        _, actions = plan([(ecodes.EV_KEY, PTT, 1)], CAPS, WATCH)
        assert actions[0].code == PTT
        assert actions[0].pressed is True

    def test_remapping_the_ptt_key_does_not_move_ptt(self):
        """The whole reason actions come from the raw batch."""
        table = build_remap({"KEY_GRAVE": "KEY_F13"})
        forwarded, actions = plan([(ecodes.EV_KEY, PTT, 1)], table, WATCH)
        assert forwarded == [(ecodes.EV_KEY, code("KEY_F13"), 1)]
        assert actions == [KeyAction(PTT, True)]

    def test_panic_is_reported_on_its_own_key(self):
        _, actions = plan([(ecodes.EV_KEY, PANIC, 1)], CAPS, WATCH)
        assert actions[0].code == PANIC

    def test_unwatched_keys_produce_no_action(self):
        _, actions = plan([(ecodes.EV_KEY, code("KEY_A"), 1)], CAPS, WATCH)
        assert actions == []

    def test_repeat_is_forwarded_but_not_acted_on(self):
        events = [(ecodes.EV_KEY, code("KEY_CAPSLOCK"), 2)]
        forwarded, actions = plan(events, CAPS, WATCH)
        assert forwarded == [(ecodes.EV_KEY, code("KEY_ESC"), 2)]
        assert actions == []

    def test_repeat_can_be_asked_for(self):
        events = [(ecodes.EV_KEY, PTT, 2)]
        _, actions = plan(events, CAPS, WATCH, ignore_repeat=False)
        assert actions[0].pressed is True

    def test_release_is_not_a_press(self):
        _, actions = plan([(ecodes.EV_KEY, PTT, 0)], CAPS, WATCH)
        assert actions[0].pressed is False

    def test_syn_and_scan_pass_through_untouched(self):
        events = [
            (ecodes.EV_MSC, code("MSC_SCAN"), 0x70065),
            (ecodes.EV_SYN, 0, 0),
        ]
        forwarded, actions = plan(events, CAPS, WATCH)
        assert forwarded == events
        assert actions == []

    def test_batch_keeps_order(self):
        events = [
            (ecodes.EV_KEY, code("KEY_CAPSLOCK"), 1),
            (ecodes.EV_KEY, PTT, 1),
            (ecodes.EV_KEY, code("KEY_CAPSLOCK"), 0),
            (ecodes.EV_KEY, PTT, 0),
        ]
        forwarded, _ = plan(events, CAPS, WATCH)
        assert [c for _, c, _ in forwarded] == [
            code("KEY_ESC"),
            PTT,
            code("KEY_ESC"),
            PTT,
        ]

    def test_empty_batch(self):
        assert plan([], CAPS, WATCH) == ([], [])


class _Pipe:
    """A real readable fd, so select() and the liveness check behave."""

    def __init__(self) -> None:
        self._read, self._write = os.pipe()
        os.write(self._write, b"x")

    @property
    def fd(self) -> int:
        return self._read

    def close(self) -> None:
        for fd in (self._read, self._write):
            with contextlib.suppress(OSError):
                os.close(fd)


class _FakeSource:
    def __init__(self, batch, pipe):
        self.batch = list(batch)
        self._pipe = pipe
        self.grabbed = False
        self.written = []
        self.closed = False

    @property
    def fd(self) -> int:
        return self._pipe.fd

    def capabilities(self):
        return {ecodes.EV_KEY: [1, 2, 3], ecodes.EV_LED: [0]}

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def read(self):
        batch, self.batch = self.batch, []
        with contextlib.suppress(OSError):
            os.read(self._pipe.fd, 1)
        return [_Ev(*e) for e in batch]

    def write(self, ev_type, code, value):
        self.written.append((ev_type, code, value))

    def close(self):
        self.closed = True
        self._pipe.close()


class _Ev:
    def __init__(self, type, code, value):
        self.type = type
        self.code = code
        self.value = value


class _FakeVirtual:
    def __init__(self, pipe):
        self._pipe = pipe
        self.written = []
        self.syns = 0
        self.closed = False

    @property
    def fd(self) -> int:
        return self._pipe.fd

    def read(self):
        with contextlib.suppress(OSError):
            os.read(self._pipe.fd, 1)
        return []

    def write(self, ev_type, code, value):
        self.written.append((ev_type, code, value))

    def syn(self):
        self.syns += 1

    def close(self):
        self.closed = True
        self._pipe.close()


def _router(batch, **kwargs):
    from src.periferia.modules.router import KeyboardRouter

    src = _FakeSource(batch, _Pipe())
    vir = _FakeVirtual(_Pipe())
    ptt = kwargs.pop("ptt_code", PTT)
    router = KeyboardRouter([], ptt, table={}, **kwargs)
    return router, src, vir


class TestPollWiring:
    def test_callbacks_see_physical_keys_and_desktop_sees_the_table(self):
        seen = []
        router, src, vir = _router(
            [(ecodes.EV_KEY, PTT, 1), (ecodes.EV_KEY, PTT, 0), (ecodes.EV_KEY, PANIC, 1)],
            ptt_code=PTT,
            panic_code=PANIC,
        )
        router._channels = [_Channel(Path("/dev/input/event6"), src, vir, grabbed=True)]
        router.table = CAPS
        router.on_press = lambda: seen.append("press")
        router.on_release = lambda: seen.append("release")
        router.on_panic = lambda: seen.append("panic")

        router.poll(timeout=0.05)

        assert seen == ["press", "release", "panic"]
        assert vir.syns == 1
        assert [c for _, c, _ in vir.written] == [PTT, PTT, PANIC]

    def test_remapped_key_reaches_the_desktop_only(self):
        router, src, vir = _router(
            [(ecodes.EV_KEY, code("KEY_CAPSLOCK"), 1), (ecodes.EV_KEY, code("KEY_CAPSLOCK"), 0)],
            ptt_code=PTT,
        )
        router._channels = [_Channel(Path("/dev/input/event6"), src, vir, grabbed=True)]
        router.table = CAPS
        router.poll(timeout=0.05)
        assert vir.written == [
            (ecodes.EV_KEY, code("KEY_ESC"), 1),
            (ecodes.EV_KEY, code("KEY_ESC"), 0),
        ]

    def test_close_releases_the_grab_and_removes_the_device(self):
        router, src, vir = _router([], ptt_code=PTT)
        router._channels = [_Channel(Path("/dev/input/event6"), src, vir, grabbed=True)]
        router.close()
        assert src.grabbed is False
        assert vir.closed is True
        assert src.closed is True
        assert router._channels == []

    def test_repeat_typing_reaches_the_desktop(self):
        router, src, vir = _router(
            [(ecodes.EV_KEY, code("KEY_CAPSLOCK"), 2)], ptt_code=PTT
        )
        router._channels = [_Channel(Path("/dev/input/event6"), src, vir, grabbed=True)]
        router.table = CAPS
        router.poll(timeout=0.05)
        assert vir.written == [(ecodes.EV_KEY, code("KEY_ESC"), 2)]
