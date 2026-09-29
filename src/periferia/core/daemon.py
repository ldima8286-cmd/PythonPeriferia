"""The daemon: wires input to audio and holds state."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections import deque
from types import FrameType

from ..modules.audio import VirtualMic
from ..modules.hotkey import (
    HotkeyListener,
    find_keyboards,
    is_button_code,
    resolve_key,
)
from ..modules.processing import MicProcessing
from ..modules.sessionlock import SessionLockWatcher
from . import config as config_mod
from . import logging_setup

log = logging.getLogger(__name__)

TRAY_CLOSED = 1
TRAY_OPEN = 2
TRAY_PANIC = 3


class Daemon:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.mic = VirtualMic(cfg.audio)
        self.processing = MicProcessing(cfg.processing)
        self._release_at: float | None = None
        self._pressed_at: float | None = None
        self._lock: SessionLockWatcher | None = None
        self._stop = threading.Event()
        self._listener: HotkeyListener | None = None
        # Timestamps of the last few state changes, so a caller can tell what
        # actually happened instead of inferring it from the output. Written
        # from the listener thread, read from anywhere.
        self._events: deque[tuple[float, str]] = deque(maxlen=64)
        self._events_lock = threading.Lock()

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

    def setup(self) -> bool:
        if not self.cfg.audio.enabled:
            log.info("audio module disabled, nothing to do")
            return False

        physical = self.mic.pick_physical()
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

        self._listener = HotkeyListener(
            devices,
            code,
            panic_code=panic,
            ignore_repeat=self.cfg.ptt.ignore_repeat,
            on_press=self.on_press,
            on_release=self.on_release,
            on_panic=self.on_panic,
        )
        return True

    def on_press(self) -> None:
        log.info("ptt down -> mic open")
        self._release_at = None
        self._pressed_at = self._record("press")
        self.mic.open_mic()

    def on_release(self) -> None:
        # logging this too: with only the press side visible, a listener that
        # never saw anything was indistinguishable from a key that was never
        # pressed, and that cost a long hunt
        log.info("ptt up -> mic closes after %d ms", max(0, self.cfg.audio.hold_ms))
        self._record("release")
        self._pressed_at = None
        hold = max(0, self.cfg.audio.hold_ms) / 1000.0
        self._release_at = time.monotonic() + hold

    def on_panic(self) -> None:
        log.warning("panic pressed")
        self._record("panic")
        self._release_at = None
        self.mic.panic()

    def on_session_locked(self) -> None:
        """Close the microphone without waiting for a key that may not come."""
        self._record("lock")
        self.force_release("session locked")

    def force_release(self, why: str) -> None:
        """Shut the mic now and forget that the key was held.

        The key itself is not grabbed, so a release event can be lost when a
        session locks. Clearing the state here is what stops the next press
        from being swallowed as a duplicate.
        """
        if self._release_at is None and self._pressed_at is None:
            return
        log.info("forcing the microphone closed: %s", why)
        self._release_at = None
        if self._listener is not None:
            self._listener.reset_state()
        self.mic.force_silence()

    def _expire_stuck_hold(self) -> None:
        """Last resort if a press never gets a matching release.

        A key repeat storm, an unplugged receiver or a compositor bug can all
        leave the key logically down. The design assumes the mic must not stay
        open indefinitely, so a long hold is cut off.
        """
        if self._pressed_at is None:
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

    def run(self) -> int:
        if not self.setup():
            return 1

        listener = self._listener
        assert listener is not None
        opened = False
        locked_watch = False

        try:
            try:
                listener.open()
                opened = True
            except (OSError, RuntimeError) as exc:
                log.error("cannot open the input device: %s", exc)
                log.error("this usually means missing permissions, see docs/udev.md")
                return 1

            self._watch_session_lock()
            locked_watch = self._lock is not None

            log.info(
                "periferia is running, mic %s, press Ctrl+C to stop", self.mic.source
            )

            while not self._stop.is_set():
                listener.poll(timeout=0.2)
                if self._lock is not None:
                    self._lock.poll()
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
            if locked_watch and self._lock is not None:
                self._lock.stop()
                self._lock = None
            self.mic.force_silence()
            self.processing.stop()
            self.mic.teardown()
        return 0

    def _watch_session_lock(self) -> None:
        if not self.cfg.ptt.release_on_lock:
            log.info("release_on_lock is off, a locked session will not close the mic")
        else:
            self._lock = SessionLockWatcher(self.on_session_locked)
            if not self._lock.start():
                self._lock = None
                log.info("continuing without screen lock protection")

    def stop(self, signum: int, frame: FrameType | None) -> None:
        log.info("signal %s, shutting down", signum)
        self._stop.set()


def main() -> int:
    cfg = config_mod.load()
    logging_setup.setup(cfg.log)
    daemon = Daemon(cfg)
    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
