"""Plays a macro onto a virtual keyboard, and can be stopped mid-word.

The device exists for one reason that is easy to get wrong: the steps go out as
a *new* virtual keyboard rather than being written into the real one. Writing
into a real device's event node is not something evdev supports, and stealing
one would mean grabbing the user's physical keyboard to replay two letters.

Stopping matters more than playing. A macro that types is the one thing this
program does that cannot be taken back once it is on screen, so `stop` is
checked before every event and the wait between events is interruptible. A macro
with a five second gap in the middle is exactly the case where a user reaches
for the panic key and expects it to work.

Playback walks a timeline rather than a list of steps. A step is one key going
down and coming back up at a known offset from the start, and the press and the
release are separate events in that timeline, which is what lets a modifier and
a letter be down together instead of being flattened into two taps.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Sequence

from evdev import UInput, ecodes

from ..core.macro import Step, timeline

log = logging.getLogger("periferia.macro")

VIRTUAL_NAME = "Periferia macro keys"
VENDOR = 0x1209
PRODUCT = 0x0001

# Playback is intentionally unhurried. A step whose hold is shorter than the
# kernel's own repeat delay can be seen by the compositor as a key that was never
# released, which some apps read as a stuck modifier.
MIN_HOLD_S = 0.004
# The same floor in the timeline's own milliseconds, so a release can be pushed
# back without converting through seconds and losing sub-millisecond precision.
MIN_HOLD_MS = 4
# How long a replaced macro gets to notice the stop and let go before the new
# one is refused. Waits are interruptible now, so this is a backstop rather than
# the usual path.
REPLACE_TIMEOUT_S = 2.0


class MacroPlayer:
    """Plays steps on a background thread, stopping the moment it is asked to."""

    def __init__(self, sleep: Callable[[float], None] | None = None) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device: UInput | None = None
        self._codes: set[int] = set()
        # Offset at which each currently held key went down, so a release can be
        # held back long enough to clear the kernel's repeat delay.
        self._pressed_at: dict[int, int] = {}
        # Injectable so a test can play a whole macro without waiting for it.
        # When one is supplied _pause calls it straight through, which is what
        # keeps tests instant.
        self._injected_sleep = sleep

    def _pause(self, seconds: float) -> bool:
        """Wait, but wake up the instant stop() is called. Returns False if asked
        to stop.

        Plain time.sleep here was the reason panic felt unreliable: it is only
        checked between steps, so stopping during a five second gap left the
        macro pressing keys for another five seconds after the panic. Waiting on
        the stop event means the wait itself is what gets cut short.
        """
        if self._injected_sleep is not None:
            self._injected_sleep(seconds)
            return not self._stop.is_set()
        return not self._stop.wait(seconds)

    @property
    def playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Ask for the current playback to end. Safe from any thread."""
        self._stop.set()

    def close(self) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with contextlib.suppress(Exception):
            if self._device is not None:
                self._device.close()
        self._device = None

    def _ensure_device(self) -> UInput:
        if self._device is None:
            # Claiming only the keys the macro uses keeps the virtual keyboard
            # out of the way of everything else, and keeps hotplug managers from
            # offering to configure a device with two hundred capabilities.
            self._device = UInput(
                events={ecodes.EV_KEY: sorted(self._codes) or [ecodes.KEY_A]},
                name=VIRTUAL_NAME,
                vendor=VENDOR,
                product=PRODUCT,
            )
        return self._device

    def play(self, steps: Sequence[Step], *, replace: bool = False) -> bool:
        """Start playing. Returns False when something is already playing.

        Refusing a second playback rather than queueing it is deliberate: a macro
        that replays itself, or a trigger that repeats because it was held, would
        otherwise build a backlog that keeps typing long after either was released.

        `replace` cuts the current one short and starts this one. That is what a
        person pressing a second macro key means: do this instead, not after.
        The current thread is waited for, because otherwise it would still be
        alive when the new one starts and both would type at once.
        """
        if not steps:
            return False
        if self.playing:
            if not replace:
                log.warning("already playing, refusing to start a second macro")
                return False
            self.stop()
            if self._thread is not None:
                self._thread.join(timeout=REPLACE_TIMEOUT_S)
            if self.playing:
                log.warning("current macro would not stop, refusing to replace it")
                return False
        self._stop.clear()
        self._codes = {step.code for step in steps}
        self._thread = threading.Thread(
            target=self._run,
            args=(tuple(steps),),
            name="periferia-macro",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, steps: tuple[Step, ...]) -> None:
        device = self._ensure_device()
        held: set[int] = set()
        try:
            previous = 0
            for at_ms, code, value in timeline(steps):
                due = at_ms
                if value == 0:
                    # A release must not go out sooner than the kernel's repeat
                    # delay after its own press, even when the recording says the
                    # key was up for a moment. Some apps read that pair as one key
                    # that never came back up and keep the modifier held for
                    # everything typed after it.
                    due = max(due, self._pressed_at.get(code, at_ms) + MIN_HOLD_MS)
                wait = (due - previous) / 1000.0
                if wait > 0 and not self._pause(wait):
                    log.info("macro playback stopped")
                    return
                device.write(ecodes.EV_KEY, code, value)
                device.syn()
                if value == 1:
                    held.add(code)
                    self._pressed_at[code] = at_ms
                else:
                    held.discard(code)
                    self._pressed_at.pop(code, None)
                previous = max(previous, at_ms)
            log.info("macro finished")
        except OSError:
            log.exception("macro playback failed")
        finally:
            # Keys left down stay down on the real desktop too: every window
            # that saw them believes they are still held. This runs even when
            # the stop arrived as an exception, which is when it matters most,
            # and it walks every held key because a chord can be holding a
            # modifier and a letter at once.
            for code in sorted(held):
                with contextlib.suppress(Exception):
                    device.write(ecodes.EV_KEY, code, 0)
                    device.syn()
                    log.warning("released key %d that was still held", code)
            self._pressed_at.clear()
            held.clear()
