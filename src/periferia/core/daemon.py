"""The daemon: wires input to audio and holds state."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from types import FrameType

from ..core import keyboards as keyboards_mod
from ..core import pipewire as pipewire_mod
from ..core import windowprofile as windowprofile_mod
from ..core import windowwatch as windowwatch_mod
from ..modules.audio import VirtualMic
from ..modules.hotkey import (
    HotkeyListener,
    find_keyboards_detailed,
    is_button_code,
    key_label,
    resolve_key,
)
from ..modules.macrodevice import MacroPlayer
from ..modules.processing import MicProcessing
from ..modules.remap import RemapError, active_remap, remap_for
from ..modules.router import KeyboardRouter
from . import config as config_mod
from . import logging_setup
from . import state as state_mod
from . import watcher as watcher_mod
from .macro import Macro, bindings, effective

log = logging.getLogger(__name__)


def _remapping(listener: HotkeyListener | KeyboardRouter) -> bool:
    """Whether a listener applies a keyboard profile rather than just reading keys.

    Asked as a property rather than isinstance() because the plain listener is
    the fallback, and what the daemon needs to know is what a listener will do,
    not which class it happens to be.
    """
    return bool(getattr(listener, "remapping", False))


def _log_disabled(table: dict[int, int | None] | None) -> None:
    """Name the keys a profile turned off.

    A disabled key has no way to announce itself: it simply stops existing, and
    the only way to find that out is to remember that it used to. So it is said
    out loud once, when the profile is applied.
    """
    killed = [key_label(code) for code, target in (table or {}).items() if target is None]
    if killed:
        log.info("keys turned off in this profile: %s", ", ".join(sorted(killed)))


def _listener_table(
    listener: HotkeyListener | KeyboardRouter,
) -> dict[int, int | None] | None:
    """The remap table a listener is actually applying, or None if it is not one."""
    if not _remapping(listener):
        return None
    return dict(getattr(listener, "table", {}))


class Daemon:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.mic = VirtualMic(cfg.audio, rebuild=self._rebuild_source)
        self.processing = MicProcessing(cfg.processing)
        # Rebuilding drops the node applications are pointed at, so it is not
        # done on every key press. A burst of presses with the source missing
        # gets one rebuild, not one per press.
        self._rebuilt_at: float = 0.0
        self._release_at: float | None = None
        self._pressed_at: float | None = None
        # True when the mic is held open by a latch and the key is no longer
        # down. max_press_ms must not fire in that state, or it would cut off
        # a mic nobody is holding down.
        self._latched = False
        self._stop = threading.Event()
        self._listener: HotkeyListener | KeyboardRouter | None = None
        self.macros = MacroPlayer()
        # Keyboards seen before, so a rescan can tell a newly plugged one from
        # one already being watched, and so the same keyboard stays first.
        self._known = keyboards_mod.KnownKeyboards()
        self._watched: set[Path] = set()
        # Whether the PTT key is a mouse button, which decides if mice are worth
        # looking for. Cached from the resolved code rather than re-resolved on
        # every rescan.
        self._include_pointers = False
        # The remap table currently in force, so a config reload can tell an
        # actual profile change from a save that only touched a macro.
        self._loaded_table: dict[int, int | None] | None = None
        # Whether the listener currently holds the devices open. A config reload
        # rebuilds the listener and has to know whether it must reopen it.
        self._listener_open = False
        # Follows the focused window so a profile can claim itself, and the name
        # of the one in force, so a switch does not rebuild what is already up.
        self._windows: windowwatch_mod.WindowWatcher | None = None
        self._window_profile: str | None = None
        # Watches the config file, so a finished macro recording goes live
        # without restarting the daemon.
        self._config_watcher: watcher_mod.ConfigWatcher | None = None
        # Bound macros, resolved once at start. A macro whose key name is not a
        # real key is left out here and reported by 'periferia macro check'
        # instead of taking the daemon down on the next keypress.
        self._macros_by_name: dict[str, Macro] = {}
        self._macro_codes: dict[int, str] = {}
        # Timestamps of the last few state changes, so a caller can tell what
        # actually happened instead of inferring it from the output. Written
        # from the listener thread, read from anywhere.
        self._events: deque[tuple[float, str]] = deque(maxlen=64)
        self._events_lock = threading.Lock()

    def load_macros(self) -> int:
        """Resolve the macros this config binds to keys. Returns how many are
        playable.

        The global set is merged with the first enabled profile, which is how
        the rest of the program picks a profile. Anything unresolvable is
        dropped here and reported by 'macro check' rather than raising, because
        a typo in one macro must not stop the microphone from starting.
        """
        from ..modules.hotkey import code_name, resolve_key

        def resolve(name: str) -> int | None:
            try:
                code: int | None = resolve_key(str(name).strip().upper())
            except Exception:
                return None
            return code

        try:
            live = effective(self.cfg.macros, self.cfg.profiles, resolve)
        except Exception as exc:
            log.warning("macros could not be loaded: %s", exc)
            self._macros_by_name = {}
            self._macro_codes = {}
            return 0
        self._macros_by_name = {m.name: m for m in live}
        # bindings() returns a list per key on purpose, so two macros on one key
        # stay visible. Which one wins is decided once here instead of inside a
        # keypress, and the loser is logged, because silently playing the other
        # one is how a wrong macro looks like a ghosting bug.
        self._macro_codes = {}
        for code, on_key in bindings(live, resolve).items():
            if len(on_key) > 1:
                log.warning(
                    "key %s has %d macros on it (%s); using %s",
                    code_name(code),
                    len(on_key),
                    ", ".join(m.name for m in on_key),
                    on_key[-1].name,
                )
            self._macro_codes[code] = on_key[-1].name
        if self._macro_codes:
            log.info("%d macro(s) bound to keys", len(self._macro_codes))
        return len(self._macro_codes)

    def reload_config(self) -> bool:
        """Re-read the config and apply it to macros and to the keyboard profile.

        What is deliberately not reloaded: the microphone, the virtual source and
        the audio processing. Those own a PipeWire node that applications are
        pointed at, and rebuilding it drops every stream pointed at it. A macro
        or a profile change needs none of that, so a reload here can never take
        the microphone away from a game that is already using it.

        A broken config is the important case. The recording left the file is the
        usual reason, so the old macros and the old profile stay loaded and the
        reason is logged; the alternative, dropping everything, would silence the
        microphone on a typo in one key name.
        """
        path = self.cfg.path
        if path is None:
            log.debug("no config path, cannot reload")
            return False

        old_profiles = self.cfg.profiles
        try:
            fresh = config_mod.load(path)
        except Exception as exc:
            log.error("config is not readable, keeping what is loaded: %s", exc)
            return False

        self.cfg = fresh
        self.load_macros()

        # A reload that adds a match wants the watcher, and one that removes the
        # last match wants it gone. Decided on the config rather than on whether a
        # watcher happens to be running, so the two cannot disagree.
        if windowwatch_mod.wants_window(fresh.profiles) != windowwatch_mod.wants_window(
            old_profiles
        ):
            self._stop_window_watcher()
            self._start_window_watcher()

        old_table = self._loaded_table
        try:
            table = self._table_for_now()
        except RemapError as exc:
            # Not the same as a table of None. None means the config asks for no
            # remapping and that is obeyed; a table that cannot be built is a
            # mistake in the file, and obeying half of it would turn a typo into
            # keys quietly going back to normal.
            log.error("ignoring the keyboard profile: %s", exc)
            return False

        if table == old_table:
            log.info("macros reloaded, the keyboard profile did not change")
            return True

        # Only the keyboard is rebuilt, and only when the remap table actually
        # changed. Saving a macro must not cost the user their keyboard for a
        # second, and the profile is the expensive part.
        if not self._swap_profile(table):
            log.warning("cannot apply the new keyboard profile, keeping the old one")
            self._swap_profile(old_table)
            return False

        if self._loaded_table is None and table is not None:
            log.warning("the new keyboard profile did not take, running without it")
        return True

    def _table_for_now(self) -> dict[int, int | None] | None:
        """The table that should be in force right now.

        The focused window decides when it can be asked, and the first enabled
        profile when it cannot. The two are the same call for anyone who never
        writes a match, so a config that only wants a profile by hand does not
        have to know any of this.
        """
        window = self._windows.last if self._windows is not None else None
        if window is not None and window.has_identifier():
            profile = windowprofile_mod.select(self.cfg.profiles, window)
            if profile is not None:
                return remap_for(profile)
            return None
        return active_remap(self.cfg.profiles)

    def _swap_profile(self, table: dict[int, int | None] | None) -> bool:
        """Rebuild the listener for a different remap table.

        `table` of None means "leave the keys alone", which is what a profile
        with no remap in it means and what a window nothing claims means.

        The old listener is kept until the new one is actually open. A reload that
        left no way to read the keyboard would take push-to-talk with it, and a
        config the user just typed is the least trustworthy thing to bet the
        microphone on.
        """
        current = self._listener
        assert current is not None
        devices = list(current.devices)
        code = current.ptt_code
        panic = current.panic_code
        was_open = self._listener_open

        replacement = self._make_router(devices, code, panic, table)
        if was_open:
            self._close_listener()
            try:
                replacement.open()
            except Exception as exc:
                log.error("cannot remap the keyboard, continuing without it: %s", exc)
                plain = self._make_plain(devices, code, panic)
                self._listener = plain
                plain.open()
                self._loaded_table = None
                self._listener_open = True
                return True
            # Closing the old one cleared the flag, and a flag left false here
            # makes the next swap skip the open, which silently costs the user
            # the keyboard on the second window change rather than the first.
            self._listener_open = True
            if _remapping(replacement):
                log.info("keyboard profile is active")
            else:
                log.info("keyboard profile is no longer active")

        self._listener = replacement
        self._loaded_table = _listener_table(replacement)
        return True

    def _start_window_watcher(self) -> None:
        """Follow the focused window, if any profile asks to be chosen that way.

        Asked for nothing but a single profile with no match, no script is loaded
        into the compositor: there is nothing for it to decide, and a config the
        user wrote once should not leave anything running in their session.
        """
        if not windowwatch_mod.wants_window(self.cfg.profiles):
            return

        watcher = windowwatch_mod.WindowWatcher()
        if not watcher.start():
            log.warning("cannot follow the focused window: %s", watcher.error)
            log.warning("profiles keep using the first enabled one")
            return

        self._windows = watcher
        self._window_profile = None
        self._follow_window()

    def _follow_window(self) -> None:
        """Pick the profile for the window that has focus and apply it.

        Nothing is done when the chosen profile is the one already in force.
        Applying a profile grabs every keyboard, so a switch that lands on the
        profile already loaded would interrupt typing for no visible reason.
        """
        watcher = self._windows
        if watcher is None:
            return
        window = watcher.drain()
        if window is None:
            return

        profile = windowprofile_mod.select(self.cfg.profiles, window)
        if profile is None:
            if self._window_profile is not None:
                log.info("no profile claims that window, leaving keys alone")
                self._window_profile = None
                self._swap_profile(None)
            return

        name = getattr(profile, "name", "")
        if name == self._window_profile:
            return

        try:
            table = remap_for(profile)
        except RemapError as exc:
            log.error("profile %s cannot be applied: %s", name, exc)
            return

        if self._swap_profile(table):
            self._window_profile = name
            log.info("profile %s is active", name)

    def _stop_window_watcher(self) -> None:
        if self._windows is not None:
            self._windows.stop()
            self._windows = None
            self._window_profile = None

    def _open_listener(self) -> None:
        """Open the current listener, falling back to plain listening if needed.

        A profile that cannot be applied is not worth the microphone. If the grab
        or the virtual device fails, push-to-talk carries on unmodified and the
        reason is logged, rather than the daemon refusing to start.
        """
        listener = self._listener
        assert listener is not None

        if _remapping(listener):
            try:
                listener.open()
            except Exception as exc:
                log.error("cannot remap the keyboard, continuing without it: %s", exc)
                listener = self._make_plain(
                    list(listener.devices), listener.ptt_code, listener.panic_code
                )
                self._listener = listener
                listener.open()
                self._loaded_table = None
                self._listener_open = True
                return
            self._loaded_table = _listener_table(listener)
            log.info("keyboard profile is active")
            _log_disabled(self._loaded_table)

        listener.open()
        self._listener_open = True

    def _close_listener(self) -> None:
        listener = self._listener
        if listener is None:
            return
        listener.close()
        self._listener_open = False

    def _reload_from_watcher(self) -> None:
        """Apply a changed config, then reset the watcher.

        The reload replaces the listener object, and the loop polls whichever one
        is current, so a reload that swapped it is followed immediately by the new
        one. forget() keeps the change from being seen twice, which for a profile
        change would mean rebuilding the keyboard for nothing.
        """
        self.reload_config()
        if self._config_watcher is not None:
            self._config_watcher.forget()

    def _macro_triggers(self) -> dict[int, Callable[[], None]]:
        return {
            code: partial(self.play_macro, name)
            for code, name in self._macro_codes.items()
        }

    def play_macro(self, name: str) -> None:
        """Play one macro by name, replacing whatever was playing.

        No queue. A macro key pressed five times should end with one macro
        halfway through, not five macros left to run after the user has already
        moved on to something else.
        """
        macro = self._macros_by_name.get(name)
        if macro is None:
            log.debug("macro %s is not bound to a key", name)
            return
        if not self.macros.play(macro.steps, replace=True):
            log.warning("macro %s could not start", name)
            return
        self._record(f"macro:{name}")

    def _record(self, name: str) -> float:
        now = time.monotonic()
        with self._events_lock:
            self._events.append((now, name))
        return now

    def events(self, name: str, since: float = 0.0) -> list[float]:
        """Timestamps of a named event at or after 'since', newest first.

        Both use time.monotonic, so a caller can hand in the moment it started
        watching and get back only what happened after that.
        """
        with self._events_lock:
            return [t for t, n in reversed(self._events) if n == name and t >= since]

    def _rebuild_source(self) -> str | None:
        """Load the echo-cancel module again and hand back the new source name.

        Called by the gate when the name it holds stops resolving. The node
        behind the name is gone at that point, and no amount of retrying the
        old name will bring it back, so the chain is put up again from
        scratch. Rate limited: a key held down produces a press per repeat.
        """
        if time.monotonic() - self._rebuilt_at < 5.0:
            return None
        self._rebuilt_at = time.monotonic()

        physical = self.mic.pick_physical()
        if not physical:
            log.error("no capture device to rebuild the source from")
            return None
        log.warning("%s is gone, loading it again", self.mic.source)
        self.processing.stop()
        virtual = self.processing.start(physical, name=self.cfg.audio.virtual_name)
        if not virtual:
            log.error("could not load the source again, PTT is not working")
            return None
        log.info("source rebuilt as %s", virtual)
        return virtual

    def setup(self) -> bool:
        if not self.cfg.audio.enabled:
            log.info("audio module disabled, nothing to do")
            return False

        # The daemon usually starts alongside the sound server, and winning
        # that race used to end the process with a refused connection. Waiting
        # costs a second on a good start and saves the session on a bad one.
        if not pipewire_mod.wait_for_server():
            log.error("no sound server after waiting; is pipewire running?")
            return False

        try:
            physical = self.mic.pick_physical()
        except pipewire_mod.PipeWireError as exc:
            log.error("could not list capture devices: %s", exc)
            return False
        if not physical:
            log.error("no physical capture source found")
            return False

        # module-echo-cancel produces the virtual source itself. It used to be
        # started here and then copied through a module-loopback, but the
        # loopback created no node on WirePlumber 0.5 and the daemon died on
        # every start. The module can name its own output, so it is the whole
        # chain now.
        virtual = self.processing.start(physical, name=self.cfg.audio.virtual_name)
        if not virtual:
            log.warning(
                "could not create %s, falling back to the raw %s. "
                "The physical microphone cannot be gated, so PTT will not work",
                self.cfg.audio.virtual_name,
                physical,
            )
            virtual = physical

        if not self.mic.attach(virtual):
            log.error("could not attach the gate to %s", virtual)
            self.processing.stop()
            return False

        if self.cfg.audio.start_muted:
            self.mic.force_silence()
        state_mod.write(state_mod.CLOSED, source=self.mic.source)

        if not self._setup_hotkey():
            log.error("could not set up the hotkey, microphone stays closed")
            self.mic.teardown()
            self.processing.stop()
            return False
        return True

    def _setup_hotkey(self) -> bool:
        if not self.cfg.ptt.enabled:
            log.info("ptt module disabled, use the CLI to open the mic manually")
            return False

        code = resolve_key(self.cfg.ptt.ptt_key)
        if code is None and self.cfg.ptt.ptt_key == "auto":
            log.error(
                "ptt_key is 'auto': run 'periferia pick-key' and paste the result "
                "into the config"
            )
            return False
        if code is None:
            log.error("cannot resolve ptt_key %r", self.cfg.ptt.ptt_key)
            return False

        # a mouse only gets watched when the PTT key is one of its buttons,
        # since reading one needs an extra udev rule nobody asked for
        devices = self._ordered_devices(is_button_code(code))
        if not devices:
            log.error("no keyboard device found")
            log.error("run 'periferia check' to see what is visible and unreadable")
            return False

        panic = resolve_key(self.cfg.ptt.panic_key) if self.cfg.ptt.panic_key else None

        try:
            table = active_remap(self.cfg.profiles)
        except RemapError as exc:
            log.error("ignoring the keyboard profile: %s", exc)
            table = None
        self._remember_watched(devices)
        self._listener = self._make_router(devices, code, panic, table)
        # Only claimed as loaded once the listener is actually open, which is
        # _open_listener's job; it clears this again if remapping turns out to be
        # impossible.
        self._loaded_table = None
        return True

    def _ordered_devices(self, include_pointers: bool) -> list[Path]:
        """The keyboards to watch, ones used recently first.

        Discovery hands them over in filesystem order, which moves around when
        hardware is plugged in. Sorting by when a keyboard was last seen keeps
        the same one first between runs, and every one that shows up counts as
        used so it moves to the front.
        """
        self._include_pointers = include_pointers
        found = find_keyboards_detailed(
            self.cfg.ptt.device, include_pointers=include_pointers
        )
        self._known = keyboards_mod.KnownKeyboards()
        present = {path for path, _name, _phys, _by_id in found}
        # A keyboard that is gone has to stop being remembered as watched. Event
        # node numbers are handed out in enumeration order, so the next keyboard
        # plugged in can land on the number a departed one had, and would then be
        # taken for one already open and never read at all.
        self._watched &= present

        by_id: dict[str, Path] = {}
        for path, _name, phys, link in found:
            by_id[keyboards_mod.identity(path, phys=phys, by_id=link)] = path
        devices = [by_id[i] for i in self._known.order(list(by_id)) if i in by_id]
        for path, _name, phys, link in found:
            self._known.seen(
                keyboards_mod.identity(path, phys=phys, by_id=link), node=path
            )
        self._known.flush()
        return devices

    def _new_devices(self) -> list[Path]:
        """Keyboards that showed up since startup, in remembered priority order.

        Rate limited inside KnownKeyboards, because looking means opening every
        candidate device and the main loop calls this on every idle pass.
        """
        if not self._known.rescan_due():
            return []
        found = find_keyboards_detailed(
            self.cfg.ptt.device, include_pointers=self._include_pointers
        )
        self._watched &= {path for path, _n, _p, _b in found}

        fresh: dict[str, Path] = {}
        watched = self._listener_devices()
        for path, name, phys, link in found:
            if path in watched:
                continue
            ident = keyboards_mod.identity(path, phys=phys, by_id=link)
            self._known.touch(ident, name=name)
            fresh[ident] = path
        ranked = self._known.order(list(fresh))
        out = [fresh[i] for i in ranked if i in fresh]
        if out:
            self._watched.update(out)
            self._known.flush()
        return out

    def _listener_devices(self) -> set[Path]:
        """Paths already being watched, so a rescan can tell new from old."""
        return set(self._watched)

    def _remember_watched(self, paths: Iterable[Path]) -> None:
        self._watched.update(paths)

    def _make_router(
        self,
        devices: list[Path],
        code: int,
        panic: int | None,
        table: dict[int, int | None] | None,
    ) -> HotkeyListener | KeyboardRouter:
        """Build the listener for a remap table. Opening it is the caller's job.

        Nothing here touches the devices. open() grabs them and creates the
        virtual keyboard, and doing that inside a builder made it impossible for
        the caller to tell a constructed listener from an open one, which is
        exactly the distinction a config reload needs.
        """
        if not table:
            return self._make_plain(devices, code, panic)
        return KeyboardRouter(
            devices,
            code,
            panic_code=panic,
            ignore_repeat=self.cfg.ptt.ignore_repeat,
            table=table,
            on_press=self.on_press,
            on_release=self.on_release,
            on_panic=self.on_panic,
            macro_codes=self._macro_triggers(),
            rescan=self._new_devices,
        )

    def _make_plain(
        self, devices: list[Path], code: int, panic: int | None
    ) -> HotkeyListener:
        return HotkeyListener(
            devices,
            code,
            panic_code=panic,
            ignore_repeat=self.cfg.ptt.ignore_repeat,
            on_press=self.on_press,
            on_release=self.on_release,
            on_panic=self.on_panic,
            macro_codes=self._macro_triggers(),
            rescan=self._new_devices,
        )

    def on_press(self) -> None:
        if self._latched:
            # A latched mic is toggled by the next press, not by the release.
            # Holding the key again would otherwise open a second one.
            log.info("ptt down -> unlatched")
            self._release_at = None
            self._latched = False
            self._record("unlatch")
            self.mic.force_silence()
            state_mod.write(state_mod.CLOSED, source=self.mic.source)
            return
        log.info("ptt down -> mic open")
        self._release_at = None
        self._pressed_at = self._record("press")
        self.mic.open_mic()
        state_mod.write(state_mod.OPEN, source=self.mic.source)

    def on_release(self) -> None:
        if self._latched:
            return  # latched: the key going up is the whole point
        if self._latch_now():
            return
        # logging this too: with only the press side visible, a listener that
        # never saw anything was indistinguishable from a key that was never
        # pressed, and that cost a long hunt
        log.info("ptt up -> mic closes after %d ms", max(0, self.cfg.audio.hold_ms))
        self._record("release")
        self._pressed_at = None
        hold = max(0, self.cfg.audio.hold_ms) / 1000.0
        self._release_at = time.monotonic() + hold
        # still open until the hold runs out, so the note says closing
        state_mod.write(state_mod.CLOSING, source=self.mic.source)

    def on_panic(self) -> None:
        log.warning("panic pressed")
        self._record("panic")
        self._release_at = None
        # Panic has to drop the latch too. Left set, the next press would be
        # read as an unlatch and the microphone would never open again.
        self._latched = False
        # A macro can hold keys down and wait between them. Panic that only
        # closed the microphone would leave the typing going, which is worse
        # than the situation panic was pressed to get out of.
        if self.macros.playing:
            log.warning("panic: stopping macro playback")
        self.macros.stop()
        self.mic.panic()
        state_mod.write(state_mod.PANIC, source=self.mic.source)
        # Stamp the real end of the panic, not a level the TUI polls later.
        # A polled bar cannot resolve this below its own refresh interval,
        # which is coarse enough to hide a correct panic behind a slow sample.
        self._record("panic-closed")

    def force_release(self, why: str) -> None:
        """Shut the mic now and forget that the key was held.

        The key itself is not grabbed, so a release event can be lost when a
        session locks. Clearing the state here is what stops the next press
        from being swallowed as a duplicate.
        """
        if self._release_at is None and self._pressed_at is None and not self._latched:
            return
        # A latched mic has neither a key down nor a scheduled close, so the
        # usual check would return here and leave it transmitting for good.
        log.info("forcing the microphone closed: %s", why)
        self._release_at = None
        self._latched = False
        if self._listener is not None:
            self._listener.reset_state()
        self.mic.force_silence()
        state_mod.write(state_mod.CLOSED, source=self.mic.source)

    def _latch_now(self) -> bool:
        """Latch the mic open if the key was held long enough to ask for it."""
        threshold = self.cfg.ptt.latch_ms
        if threshold <= 0 or self._pressed_at is None:
            return False
        held_ms = (time.monotonic() - self._pressed_at) * 1000.0
        if held_ms < threshold:
            return False
        log.info("held for %d ms -> latched open, press ptt again to close", int(held_ms))
        self._latched = True
        self._pressed_at = None
        self._record("latch")
        state_mod.write(state_mod.LATCHED, source=self.mic.source)
        return True

    def _expire_stuck_hold(self) -> None:
        """Last resort if a press never gets a matching release.

        A key repeat storm, an unplugged receiver or a compositor bug can all
        leave the key logically down. The design assumes the mic must not stay
        open indefinitely, so a long hold is cut off.
        """
        if self._pressed_at is None or self._latched:
            return
        limit = self.cfg.ptt.max_press_ms
        if limit <= 0:
            return
        if time.monotonic() - self._pressed_at >= limit / 1000.0:
            self.force_release(f"held for more than {limit} ms")

    def _expire_hold(self) -> None:
        if self._release_at is None:
            return
        if time.monotonic() >= self._release_at:
            self._release_at = None
            self.mic.close_mic()
            state_mod.write(state_mod.CLOSED, source=self.mic.source)

    def run(self) -> int:
        if not self.setup():
            return 1
        self.load_macros()
        self._start_window_watcher()
        self._config_watcher = watcher_mod.config_watcher(
            self.cfg.path, self._reload_from_watcher
        )
        if self._config_watcher is not None:
            log.info("watching %s for changes", self.cfg.path)

        self._listener_open = False

        try:
            try:
                self._open_listener()
            except (OSError, RuntimeError) as exc:
                log.error("cannot open the input device: %s", exc)
                log.error("this usually means missing permissions, see docs/udev.md")
                return 1

            log.info(
                "periferia is running, mic %s, press Ctrl+C to stop", self.mic.source
            )

            while not self._stop.is_set():
                # Re-read every pass rather than caching it: a config reload
                # replaces the listener object outright.
                listener = self._listener
                assert listener is not None
                listener.poll(timeout=0.2)
                self._expire_hold()
                self._expire_stuck_hold()
                self._follow_window()
                if self._config_watcher is not None:
                    self._config_watcher.check()
        except KeyboardInterrupt:
            pass
        except OSError as exc:
            log.error("input loop ended: %s", exc)
            return 1
        finally:
            # every exit has to go through here. Returning early on a failed
            # device open used to leave the echo cancel module loaded, and the
            # next run then collided with it on the source name
            if self._listener_open:
                self._close_listener()
            # Unloads the compositor script. A script left running outlives the
            # daemon and keeps reporting to a bus name nobody owns, which makes
            # the next start look broken.
            self._stop_window_watcher()
            # Releases any key a macro was holding and closes the virtual
            # device. A macro interrupted by shutdown would otherwise leave a
            # key down for the rest of the session, and nothing in the log
            # would say so.
            self.macros.close()
            self.mic.force_silence()
            state_mod.write(state_mod.CLOSED, source=self.mic.source)
            self.processing.stop()
            self.mic.teardown()
        return 0

    def stop(self, signum: int, frame: FrameType | None) -> None:
        log.info("signal %s, shutting down", signum)
        self._stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="periferia-daemon",
        description="Hold a key to open the microphone.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    args = parser.parse_args()

    # Which file is in force, said out loud. The daemon used to read whatever
    # load() found and complain about the result, so a config that was never
    # located looked exactly like a config with the wrong values in it.
    explicit = Path(args.config) if args.config else None
    found = config_mod.find_config(explicit)
    if found is None:
        log.warning("no config found, running on defaults")
    else:
        log.info("config %s", found)

    try:
        cfg = config_mod.load(explicit)
    except (OSError, ValueError) as exc:
        # A path that was asked for by name and cannot be read is a mistake
        # worth stopping for, not a reason to run on defaults and let the
        # failure resurface later as a confusing complaint about a key.
        print(f"periferia-daemon: cannot read the config: {exc}", file=sys.stderr)
        return 2

    logging_setup.setup(cfg.log)
    daemon = Daemon(cfg)
    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
