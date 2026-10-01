"""The daemon: wires input to audio and holds state."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import FrameType

from ..core import pipewire as pipewire_mod
from ..modules.audio import VirtualMic
from ..modules.hotkey import (
    HotkeyListener,
    find_keyboards,
    is_button_code,
    resolve_key,
)
from ..modules.macrodevice import MacroPlayer
from ..modules.processing import MicProcessing
from ..modules.remap import RemapError, active_remap
from ..modules.router import KeyboardRouter
from . import config as config_mod
from . import logging_setup
from . import state as state_mod
from .macro import Macro, bindings, effective

log = logging.getLogger(__name__)

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
        devices = find_keyboards(
            self.cfg.ptt.device, include_pointers=is_button_code(code)
        )
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
        if table:
            self._listener = self._make_router(devices, code, panic, table)
        else:
            self._listener = HotkeyListener(
                devices,
                code,
                panic_code=panic,
                ignore_repeat=self.cfg.ptt.ignore_repeat,
                on_press=self.on_press,
                on_release=self.on_release,
                on_panic=self.on_panic,
                macro_codes=self._macro_triggers(),
            )
        return True

    def _make_router(
        self, devices: list[Path], code: int, panic: int | None, table: dict[int, int]
    ) -> HotkeyListener | KeyboardRouter:
        """Build a router, or fall back to plain listening if it cannot start.

        A profile that cannot be applied is not worth the microphone. If the
        grab or the virtual device fails, push-to-talk carries on unmodified and
        the reason is logged, rather than the daemon refusing to start.
        """
        try:
            router = KeyboardRouter(
                devices,
                code,
                panic_code=panic,
                ignore_repeat=self.cfg.ptt.ignore_repeat,
                table=table,
                on_press=self.on_press,
                on_release=self.on_release,
                on_panic=self.on_panic,
                macro_codes=self._macro_triggers(),
            )
            router.open()
        except Exception as exc:
            log.error("cannot remap the keyboard, continuing without it: %s", exc)
            return HotkeyListener(
                devices,
                code,
                panic_code=panic,
                ignore_repeat=self.cfg.ptt.ignore_repeat,
                on_press=self.on_press,
                on_release=self.on_release,
                on_panic=self.on_panic,
                macro_codes=self._macro_triggers(),
            )
        log.info("keyboard profile is active")
        return router

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

        listener = self._listener
        assert listener is not None
        opened = False

        try:
            try:
                listener.open()
                opened = True
            except (OSError, RuntimeError) as exc:
                log.error("cannot open the input device: %s", exc)
                log.error("this usually means missing permissions, see docs/udev.md")
                return 1

            log.info(
                "periferia is running, mic %s, press Ctrl+C to stop", self.mic.source
            )

            while not self._stop.is_set():
                listener.poll(timeout=0.2)
                self._expire_hold()
                self._expire_stuck_hold()
        except KeyboardInterrupt:
            pass
        except OSError as exc:
            log.error("input loop ended: %s", exc)
            return 1
        finally:
            # every exit has to go through here. Returning early on a failed
            # device open used to leave the echo cancel module loaded, and the
            # next run then collided with it on the source name
            if opened:
                listener.close()
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
