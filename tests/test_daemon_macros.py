"""The daemon's side of macros: loading them, firing them, and panic.

The pieces are tested elsewhere, so what matters here is the wiring and the
promises the daemon makes about a macro:

  - a macro key plays the macro the config says;
  - a second press replaces the first instead of queueing behind it;
  - panic stops a macro mid-flight, which is the whole reason it exists.

These run against a fake player, because a real one needs /dev/uinput.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from src.periferia.core import config as config_mod
from src.periferia.core import daemon as daemon_mod
from src.periferia.core import keyboards as keyboards_mod
from src.periferia.core import watcher as watcher_mod
from src.periferia.core import windowwatch as windowwatch_mod
from src.periferia.core.daemon import Daemon
from src.periferia.core.macro import Step
from src.periferia.modules.hotkey import resolve_key

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


class _FakeListener:
    """Stands in for the hotkey listener during a reload.

    Records open and close, because the thing worth testing about a profile swap
    is that the old keyboard is let go and the new one is picked up, in that order.
    """

    def __init__(self, devices, table, opened, closed):
        # table of None is what "leave the keys alone" means to the daemon, so
        # the fake has to accept it rather than dict() it.
        self.devices = list(devices)
        self.table = dict(table or {})
        self.ptt_code = 41
        self.panic_code = 88
        self.remapping = bool(table)
        self._opened = opened
        self._closed = closed

    def open(self) -> None:
        self._opened.append("open")

    def close(self) -> None:
        self._closed.append("close")


def _step(
    key: str, at_ms: int | None = None, hold_ms: int = 40
) -> config_mod.MacroStep:
    return config_mod.MacroStep(key=key, at_ms=at_ms, hold_ms=hold_ms)


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
    d._loaded_table = None
    d._listener_open = False
    d._windows = None
    d._window_profile = None
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
                steps=[_step("KEY_H"), _step("KEY_I", at_ms=50)],
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

    def test_the_offsets_from_the_recording_are_kept(self, daemon: Daemon) -> None:
        """A macro that lost its timing would type the right letters at the
        wrong speed, which is not the same macro."""
        daemon.load_macros()
        daemon._macro_triggers()[KEY_F5]()
        steps, _ = daemon.macros.calls[0]  # type: ignore[attr-defined]
        assert steps[1].at_ms == 50

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


class TestKeyboardsWithoutRestarting:
    """Which keyboards get watched, and what happens when one arrives later.

    The properties worth pinning down are that the remembered order decides what
    gets opened first, that a keyboard plugged in afterwards is picked up, and
    that neither depends on anything a test cannot control: the cache is redirected
    to a tmp path so the real one is neither read nor written.
    """

    def _daemon(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        d = _make()
        d._known = keyboards_mod.KnownKeyboards()
        d._watched = set()
        d._include_pointers = False
        return d

    def test_the_keyboard_used_most_recently_is_opened_first(
        self, tmp_path, monkeypatch
    ) -> None:
        """Otherwise the keyboard in front of you changes between boots."""
        known = keyboards_mod.KnownKeyboards()
        known.touch("phys:usb-0:1:2/input0", name="External")
        time.sleep(0.01)
        known.touch("phys:pci-0:14.0/input0", name="Laptop")
        known.flush()

        d = self._daemon(tmp_path, monkeypatch)
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (Path("/dev/input/event4"), "Laptop", "pci-0:14.0/input0", ""),
                (Path("/dev/input/event7"), "External Kbd", "usb-0:1:2/input0", ""),
            ],
        )

        # The laptop keyboard was touched last, so it comes first even though
        # discovery handed it over second.
        assert d._ordered_devices(False) == [
            Path("/dev/input/event4"),
            Path("/dev/input/event7"),
        ]

    def test_a_keyboard_plugged_in_later_is_offered_to_the_listener(
        self, tmp_path, monkeypatch
    ) -> None:
        d = self._daemon(tmp_path, monkeypatch)
        d._remember_watched([Path("/dev/input/event4")])
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (Path("/dev/input/event4"), "Laptop", "pci-0:14.0/input0", ""),
                (Path("/dev/input/event9"), "New Kbd", "usb-0:1:9/input0", ""),
            ],
        )

        assert d._new_devices() == [Path("/dev/input/event9")]

    def test_a_keyboard_already_being_watched_is_not_offered_again(
        self, tmp_path, monkeypatch
    ) -> None:
        d = self._daemon(tmp_path, monkeypatch)
        d._remember_watched([Path("/dev/input/event4")])
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (Path("/dev/input/event4"), "Laptop", "pci-0:14.0/input0", ""),
            ],
        )

        assert d._new_devices() == []

    def test_the_search_is_rate_limited(self, tmp_path, monkeypatch) -> None:
        """Looking means opening every candidate device, on every idle poll."""
        d = self._daemon(tmp_path, monkeypatch)
        calls: list[int] = []

        def counted(preferred, include_pointers=False):
            calls.append(1)
            return [(Path("/dev/input/event4"), "Laptop", "pci-0:14.0/input0", "")]

        monkeypatch.setattr(daemon_mod, "find_keyboards_detailed", counted)

        d._new_devices()
        d._new_devices()

        assert len(calls) == 1

    def test_a_new_keyboard_is_remembered_for_next_time(
        self, tmp_path, monkeypatch
    ) -> None:
        """So that the keyboard just plugged in is first on the next boot."""
        d = self._daemon(tmp_path, monkeypatch)
        d._remember_watched([Path("/dev/input/event4")])
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (Path("/dev/input/event9"), "New Kbd", "usb-0:1:9/input0", ""),
            ],
        )

        d._new_devices()

        stored = json.loads((tmp_path / "periferia" / "keyboards.json").read_text())
        assert stored["keyboards"][0]["id"] == "phys:usb-0:1:9/input0"
        assert stored["keyboards"][0]["name"] == "New Kbd"


class TestReloadingTheConfigWithoutRestarting:
    """The daemon keeps the microphone, so a config change cannot be allowed to
    take it away. Everything here is about what survives a reload."""

    def _daemon(self, tmp_path, monkeypatch, text):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        cfg = config_mod.load(path)
        d = _make(macros=list(cfg.macros), profiles=list(cfg.profiles))
        d.cfg = cfg
        d._loaded_table = None
        d._listener_open = True
        d._config_watcher = None
        opened: list[str] = []
        closed: list[str] = []
        d._listener = _FakeListener([], {}, opened, closed)
        d._make_router = (  # type: ignore[method-assign]
            lambda devices, code, panic, table: _FakeListener(devices, table, opened, closed)
        )
        return d, path, opened, closed

    def test_a_new_macro_is_playable_right_after_the_file_changes(
        self, tmp_path, monkeypatch
    ):
        d, path, _, _ = self._daemon(
            tmp_path,
            monkeypatch,
            "macros:\n"
            "  - name: hello\n"
            "    bind: KEY_F5\n"
            "    steps:\n"
            "      - key: KEY_H\n"
            "        gap_ms: 0\n",
        )
        assert d.reload_config() is True
        assert KEY_F5 in d._macro_codes

        path.write_text(
            "macros:\n"
            "  - name: hello\n"
            "    bind: KEY_F5\n"
            "    steps:\n"
            "      - key: KEY_H\n"
            "        gap_ms: 0\n"
            "  - name: bye\n"
            "    bind: KEY_F6\n"
            "    steps:\n"
            "      - key: KEY_I\n"
            "        gap_ms: 0\n",
            encoding="utf-8",
        )
        assert d.reload_config() is True
        assert KEY_F6 in d._macro_codes
        d.play_macro("bye")
        assert d.macros.calls[-1][0][0].code == KEY_I

    def test_a_macro_removed_from_the_file_stops_being_playable(
        self, tmp_path, monkeypatch
    ):
        d, path, _, _ = self._daemon(
            tmp_path,
            monkeypatch,
            "macros:\n"
            "  - name: hello\n"
            "    bind: KEY_F5\n"
            "    steps:\n"
            "      - key: KEY_H\n"
            "        gap_ms: 0\n",
        )
        path.write_text("macros: []\n", encoding="utf-8")

        assert d.reload_config() is True
        assert d._macro_codes == {}
        d.play_macro("hello")
        assert d.macros.calls == []

    def test_a_broken_config_keeps_the_macros_that_worked(
        self, tmp_path, monkeypatch
    ):
        """A half-saved file must not leave the user with no macros at all."""
        d, path, _, _ = self._daemon(
            tmp_path,
            monkeypatch,
            "macros:\n"
            "  - name: hello\n"
            "    bind: KEY_F5\n"
            "    steps:\n"
            "      - key: KEY_H\n"
            "        gap_ms: 0\n",
        )
        d.load_macros()
        path.write_text("macros: [oops\n", encoding="utf-8")

        assert d.reload_config() is False
        assert KEY_F5 in d._macro_codes
        d.play_macro("hello")
        assert len(d.macros.calls) == 1

    def test_a_changed_profile_rebuilds_the_keyboard(self, tmp_path, monkeypatch):
        d, path, opened, closed = self._daemon(
            tmp_path,
            monkeypatch,
            "profiles:\n"
            "  - name: default\n"
            "    remap:\n"
            "      KEY_CAPSLOCK: KEY_ESC\n",
        )
        d._loaded_table = {58: 1}
        path.write_text(
            "profiles:\n"
            "  - name: default\n"
            "    remap:\n"
            "      KEY_CAPSLOCK: KEY_ESC\n"
            "      KEY_F5: KEY_F6\n",
            encoding="utf-8",
        )

        assert d.reload_config() is True
        assert d._loaded_table == {58: 1, KEY_F5: KEY_F6}
        assert opened[-1] == "open"
        assert closed == ["close"]

    def test_a_macro_save_alone_does_not_touch_the_keyboard(
        self, tmp_path, monkeypatch
    ):
        """Saving a recording is the common case, and it must not cost the user
        their keyboard for a second."""
        d, path, opened, closed = self._daemon(
            tmp_path, monkeypatch,
            "profiles:\n  - name: default\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
        )
        d._loaded_table = {58: 1}
        path.write_text(
            "profiles:\n"
            "  - name: default\n"
            "    remap:\n"
            "      KEY_CAPSLOCK: KEY_ESC\n"
            "macros:\n"
            "  - name: hello\n"
            "    bind: KEY_F5\n"
            "    steps:\n"
            "      - key: KEY_H\n"
            "        gap_ms: 0\n",
            encoding="utf-8",
        )

        assert d.reload_config() is True
        assert KEY_F5 in d._macro_codes
        assert opened == []
        assert closed == []

    def test_a_profile_that_cannot_be_applied_leaves_the_old_one_in_place(
        self, tmp_path, monkeypatch
    ):
        d, path, _, _ = self._daemon(
            tmp_path,
            monkeypatch,
            "profiles:\n  - name: default\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
        )
        d._loaded_table = {58: 1}
        path.write_text(
            "profiles:\n  - name: default\n    remap:\n      KEY_CAPSLOCK: NOT_A_KEY\n",
            encoding="utf-8",
        )

        assert d.reload_config() is False
        assert d._loaded_table == {58: 1}

    def test_the_watcher_reloads_once_and_forgets_the_change(
        self, tmp_path, monkeypatch
    ):
        """For a profile change, seeing the same save twice would rebuild the
        keyboard twice."""
        d, path, opened, _closed = self._daemon(
            tmp_path,
            monkeypatch,
            "profiles:\n  - name: default\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
        )
        d._loaded_table = {58: 1}
        d._config_watcher = watcher_mod.ConfigWatcher(
            path, d._reload_from_watcher, interval=0.0
        )
        path.write_text(
            "profiles:\n  - name: default\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n"
            "      KEY_F5: KEY_F6\n",
            encoding="utf-8",
        )

        assert d._config_watcher.check() is True
        assert opened.count("open") == 1
        assert d._config_watcher.check() is False
        assert opened.count("open") == 1

class TestRememberingKeyboardsAcrossBoots:
    """Event node numbers are handed out in enumeration order, so a keyboard is
    remembered by what does not change: its physical path, or failing that its
    by-id name."""

    def _daemon(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        d = _make()
        d._known = keyboards_mod.KnownKeyboards()
        d._watched = set()
        d._include_pointers = False
        d._listener = None
        return d

    def test_a_keyboard_with_no_physical_path_is_remembered_by_its_by_id_name(
        self, tmp_path, monkeypatch
    ) -> None:
        """The event node comes back different after a reboot, so a Bluetooth
        keyboard with no phys would otherwise be forgotten every time."""
        d = self._daemon(tmp_path, monkeypatch)
        d._remember_watched([Path("/dev/input/event4")])
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (
                    Path("/dev/input/event9"),
                    "BT Kbd",
                    "",
                    "usb-Bluetooth_Kbd-if01-event-kbd",
                ),
            ],
        )

        d._new_devices()

        stored = json.loads((tmp_path / "periferia" / "keyboards.json").read_text())
        assert stored["keyboards"][0]["id"] == "byid:usb-Bluetooth_Kbd-if01-event-kbd"

    def test_an_unplugged_keyboard_stops_counting_as_watched(
        self, tmp_path, monkeypatch
    ) -> None:
        """Node numbers are reused. A keyboard that arrives later must not be
        mistaken for the one that left."""
        d = self._daemon(tmp_path, monkeypatch)
        d._remember_watched([Path("/dev/input/event7")])
        monkeypatch.setattr(
            daemon_mod,
            "find_keyboards_detailed",
            lambda preferred, include_pointers=False: [
                (Path("/dev/input/event4"), "Laptop", "pci-0:14.0/input0", ""),
            ],
        )

        d._new_devices()

        assert d._listener_devices() == {Path("/dev/input/event4")}
        assert json.loads(
            (tmp_path / "periferia" / "keyboards.json").read_text()
        )["keyboards"][0]["id"] == "phys:pci-0:14.0/input0"


class TestFollowingTheFocusedWindow:
    """Switching profiles on window change, with the compositor faked out.

    A profile switch grabs every keyboard, so what matters here is not that it
    happens but that it happens exactly once per switch.
    """

    def _daemon(self, monkeypatch, profiles):
        d = _make(profiles=list(profiles))
        d._listener_open = True
        d._config_watcher = None
        opened: list[str] = []
        closed: list[str] = []
        # Starts on the fallback profile, the way the daemon does, so a switch
        # away from it and back to it are both real rebuilds.
        d._loaded_table = _table(profiles[0].name)
        d._window_profile = profiles[0].name
        d._listener = _FakeListener([], d._loaded_table, opened, closed)
        d._make_router = (  # type: ignore[method-assign]
            lambda devices, code, panic, table: _FakeListener(devices, table, opened, closed)
        )
        d._windows = windowwatch_mod.WindowWatcher(clock=_StepClock())
        return d, opened, closed

    def _profiles(self):
        return [
            config_mod.ProfileConfig(name="default", remap={"KEY_CAPSLOCK": "KEY_ESC"}),
            config_mod.ProfileConfig(
                name="game",
                match={"resource_class": "steam"},
                remap={"KEY_CAPSLOCK": "KEY_TAB"},
            ),
        ]

    def _focus_and_poll(self, d, resource_class):
        """Put a window on the bus. The watcher holds it for SETTLE_S, so the
        poll that applies it is separate from the one that sees it."""
        d._windows._service = _OneShotService(
            {"stage": "basics", "fields": {"resourceClass": resource_class}}
        )
        d._follow_window()

    def test_switching_windows_switches_the_profile(self, monkeypatch):
        d, opened, _ = self._daemon(monkeypatch, self._profiles())
        self._focus_and_poll(d, "steam")

        d._follow_window()

        assert d._window_profile == "game"
        assert d._loaded_table == _table("game")
        assert len(opened) == 1

    def test_the_same_window_twice_does_not_rebuild_the_keyboard(self, monkeypatch):
        """Applying a profile grabs every keyboard. Doing it per report rather
        than per switch makes alt-tab stutter."""
        d, opened, _ = self._daemon(monkeypatch, self._profiles())
        self._focus_and_poll(d, "steam")
        d._follow_window()
        assert len(opened) == 1

        self._focus_and_poll(d, "steam")
        d._follow_window()

        assert len(opened) == 1
        assert d._window_profile == "game"

    def test_a_window_nothing_claims_goes_back_to_the_fallback(self, monkeypatch):
        d, opened, _ = self._daemon(monkeypatch, self._profiles())
        self._focus_and_poll(d, "steam")
        d._follow_window()
        assert d._window_profile == "game"

        self._focus_and_poll(d, "konsole")
        d._follow_window()

        assert d._window_profile == "default"
        assert d._loaded_table == _table("default")
        assert len(opened) == 2

    def test_a_profile_with_nothing_to_remap_leaves_the_keys_alone(self, monkeypatch):
        """A profile that remaps no keys is not the same as one that remaps
        them wrongly, and neither is a reason to refuse."""
        profiles = [
            config_mod.ProfileConfig(name="default"),
            config_mod.ProfileConfig(name="plain", match={"resource_class": "konsole"}),
        ]
        d, _, _ = self._daemon(monkeypatch, profiles)
        self._focus_and_poll(d, "konsole")

        d._follow_window()

        assert d._window_profile == "plain"
        assert d._loaded_table is None

    def test_a_broken_profile_does_not_change_what_is_loaded(self, monkeypatch):
        """Half a table obeyed is keys quietly going back to normal, which looks
        like a keyboard fault."""
        profiles = self._profiles()
        d, _, _ = self._daemon(monkeypatch, profiles)
        self._focus_and_poll(d, "steam")
        d._follow_window()
        good = dict(d._loaded_table or {})

        d.cfg.profiles[1].remap = {"KEY_CAPSLOCK": "NOT_A_KEY"}
        self._focus_and_poll(d, "steam")
        d._windows._service = _OneShotService(
            {"stage": "basics", "fields": {"resourceClass": "steam"}}
        )
        d._follow_window()

        # The same window, so nothing is rebuilt, and the bad table is not built.
        assert d._loaded_table == good

    def test_a_config_with_no_matching_profile_never_loads_a_script(self, monkeypatch):
        d = _make(profiles=[config_mod.ProfileConfig(name="default")])
        started: list[bool] = []
        monkeypatch.setattr(
            daemon_mod.windowwatch_mod,
            "WindowWatcher",
            lambda: _RefusingWatcher(started),
        )

        d._start_window_watcher()

        assert started == []
        assert d._windows is None

    def test_a_watcher_that_cannot_start_leaves_the_daemon_alone(self, monkeypatch):
        """Not working is reported once. It must not take push-to-talk with it."""
        profiles = [
            config_mod.ProfileConfig(name="default"),
            config_mod.ProfileConfig(name="game", match={"resource_class": "steam"}),
        ]
        d, _, _ = self._daemon(monkeypatch, profiles)
        d._windows = None
        d._window_profile = None
        d._windows = None
        monkeypatch.setattr(
            daemon_mod.windowwatch_mod,
            "WindowWatcher",
            lambda: _RefusingWatcher([], error="gdbus is not installed"),
        )

        d._start_window_watcher()

        assert d._windows is None
        assert d._window_profile is None

    def test_the_script_is_unloaded_when_the_daemon_stops(self, monkeypatch):
        """A script left loaded keeps reporting to a bus name nobody owns, which
        makes the next start of the daemon look broken."""
        d, _, _ = self._daemon(monkeypatch, self._profiles())
        watcher = _RefusingWatcher([], start_ok=True)
        d._windows = watcher

        d._stop_window_watcher()

        assert watcher.stopped == 1
        assert d._windows is None
        assert d._window_profile is None


    def test_a_reload_that_names_a_window_starts_following_it(self, monkeypatch):
        """Turning the feature on is a config edit, and asking for a restart to
        do it would make the setting look broken."""
        d, _, _ = self._daemon(monkeypatch, [config_mod.ProfileConfig(name="default")])
        d._windows = None
        started: list[bool] = []
        monkeypatch.setattr(
            daemon_mod.windowwatch_mod, "WindowWatcher", lambda: _CountingWatcher(started)
        )
        monkeypatch.setattr(
            daemon_mod.config_mod,
            "load",
            lambda _path: config_mod.Config(
                path="config.yaml",
                profiles=[
                    config_mod.ProfileConfig(name="default"),
                    config_mod.ProfileConfig(name="game", match={"resource_class": "steam"}),
                ],
            ),
        )
        d.cfg.path = "config.yaml"

        d.reload_config()

        assert started == [True]
        assert isinstance(d._windows, _CountingWatcher)

    def test_a_reload_that_removes_the_last_match_stops_following(self, monkeypatch):
        """A config left with a script loaded in the compositor is a thing the
        user has to know to clean up by hand otherwise."""
        profiles = [
            config_mod.ProfileConfig(name="default"),
            config_mod.ProfileConfig(name="game", match={"resource_class": "steam"}),
        ]
        d, _, _ = self._daemon(monkeypatch, profiles)
        watcher = _CountingWatcher([])
        d._windows = watcher
        fresh = config_mod.Config(
            path="config.yaml", profiles=[config_mod.ProfileConfig(name="default")]
        )
        monkeypatch.setattr(daemon_mod.config_mod, "load", lambda _path: fresh)
        d.cfg.path = "config.yaml"

        d.reload_config()

        assert watcher.stopped == 1
        assert d._windows is None

    def test_a_reload_that_still_matches_does_not_restart_the_script(self, monkeypatch):
        """Unloading and loading a script on every save would make editing a
        macro fight with the compositor."""
        profiles = [
            config_mod.ProfileConfig(name="default"),
            config_mod.ProfileConfig(name="game", match={"resource_class": "steam"}),
        ]
        d, _, _ = self._daemon(monkeypatch, profiles)
        watcher = _CountingWatcher([])
        d._windows = watcher
        fresh = config_mod.Config(
            path="config.yaml",
            profiles=[
                config_mod.ProfileConfig(name="default"),
                config_mod.ProfileConfig(
                    name="game",
                    match={"resource_class": "steam"},
                    remap={"KEY_CAPSLOCK": "KEY_TAB"},
                ),
            ],
        )
        monkeypatch.setattr(daemon_mod.config_mod, "load", lambda _path: fresh)
        d.cfg.path = "config.yaml"

        d.reload_config()

        assert watcher.stopped == 0
        assert d._windows is watcher


class _CountingWatcher:
    def __init__(self, starts):
        self.starts = starts
        self.stopped = 0
        self._service = None

    def start(self) -> bool:
        self.starts.append(True)
        return True

    def stop(self) -> None:
        self.stopped += 1

    @property
    def last(self):
        return None

    def drain(self):
        return None


def _table(profile_name: str) -> dict[int, int]:
    caps = resolve_key("KEY_CAPSLOCK")
    esc = resolve_key("KEY_ESC")
    tab = resolve_key("KEY_TAB")
    return {caps: esc if profile_name == "default" else tab}


class _StepClock:
    """Moves a second per reading, so the watcher's settle wait is one poll."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class _OneShotService:
    """A bus that has one message waiting and then nothing, like the real one."""

    def __init__(self, report):
        self.pending = [report]

    def next_report(self, timeout: float):
        return self.pending.pop(0) if self.pending else None

    def stop(self) -> None:
        pass


class _RefusingWatcher:
    def __init__(self, calls, *, start_ok: bool = False, error: str = ""):
        self.started_calls = calls
        self._ok = start_ok
        self.error = error
        self.started = False
        self.stopped = 0

    def start(self) -> bool:
        self.started_calls.append(True)
        self.started = self._ok
        return self._ok

    def stop(self) -> None:
        self.stopped += 1

    @property
    def last(self):
        return None

    def drain(self):
        return None


def test_a_disabled_key_is_named_in_the_log(caplog) -> None:
    """A key that stops existing is otherwise silent, and the only way to find
    it is to remember that it used to be there."""
    import logging

    from src.periferia.core.daemon import _log_disabled

    caps = resolve_key("KEY_CAPSLOCK")
    f1 = resolve_key("KEY_F1")
    with caplog.at_level(logging.INFO):
        _log_disabled({caps: None, f1: resolve_key("KEY_F13")})

    said = " ".join(record.getMessage() for record in caplog.records)
    assert "CAPSLOCK" in said
    assert "F13" not in said


def test_a_table_with_nothing_disabled_says_nothing(caplog) -> None:
    import logging

    from src.periferia.core.daemon import _log_disabled

    with caplog.at_level(logging.INFO):
        _log_disabled({resolve_key("KEY_CAPSLOCK"): resolve_key("KEY_ESC")})
        _log_disabled(None)

    assert not [r for r in caplog.records if "turned off" in r.getMessage()]
