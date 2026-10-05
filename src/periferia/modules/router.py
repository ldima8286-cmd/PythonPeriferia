"""Reads the keyboards once, applies a profile, and feeds both consumers.

Grabbing a device with EVIOCGRAB is exclusive. While this holds a keyboard, no
other process receives its events, which is exactly what a remap needs and
exactly what breaks a second reader. So the router is the only reader: it takes
the key events, hands the rewritten ones to a virtual device for the desktop,
and reports the original ones to the PTT and panic handlers.

PTT is detected on the physical key, before the table is applied. The config
already describes PTT in physical-key terms, and the alternative would mean
that remapping your push-to-talk key silently turns it off.

Anything that goes wrong leaves the keyboard alone: no virtual device is created
until the table has been validated, a failed grab tears the virtual device down
again, and a missing /dev/uinput is reported and then ignored rather than
fatal. The grab itself is released by the kernel when the file descriptor
closes, so a crash gives the keyboard back instead of bricking it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import select
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evdev import UInput, ecodes

from .hotkey import VIRTUAL_NAME_PREFIX, _fd_alive, open_device
from .remap import translate_events

log = logging.getLogger("periferia.router")

VIRTUAL_VENDOR = 0x5052
VIRTUAL_PRODUCT = 0x0001

UINPUT_PATH = Path("/dev/uinput")

_SKIP_CAPABILITIES = frozenset({ecodes.EV_SYN, ecodes.EV_FF, ecodes.EV_FF_STATUS})
_OUTPUT_TYPES = frozenset({ecodes.EV_LED, ecodes.EV_FF})


def clone_capabilities(capabilities: Mapping[int, Sequence[int]]) -> dict[int, Sequence[int]]:
    """What the virtual device should advertise.

    EV_SYN and FF are not capabilities: UInput emits sync itself, and claiming
    FF with no way to service it just invites the desktop to write effects that
    go nowhere. Everything else is copied, LED and REP included, so the desktop
    still sees the keyboard it saw before.
    """
    return {
        ev_type: list(codes)
        for ev_type, codes in capabilities.items()
        if ev_type not in _SKIP_CAPABILITIES and codes
    }


@dataclasses.dataclass(frozen=True, slots=True)
class KeyAction:
    """A press or release worth telling the application about."""

    code: int
    pressed: bool


def plan(
    events: Sequence[tuple[int, int, int]],
    table: Mapping[int, int],
    watch: frozenset[int],
    ignore_repeat: bool = True,
) -> tuple[list[tuple[int, int, int]], list[KeyAction]]:
    """Split a batch into what the desktop should see and what we should act on.

    The two halves come from the same batch but answer different questions: the
    desktop gets the table applied, the application gets what the finger did.

    Auto-repeat is always forwarded, because holding a remapped key has to keep
    typing. It just must not count as a fresh press, or holding push-to-talk
    would re-trigger it every few milliseconds. `ignore_repeat=False` asks for
    the old counting behaviour, which only makes sense if something upstream
    wants it.
    """
    actions: list[KeyAction] = []
    for ev_type, code, value in events:
        if ev_type != ecodes.EV_KEY or code not in watch:
            continue
        if ignore_repeat and value == 2:
            continue
        actions.append(KeyAction(code, value in (1, 2)))
    return translate_events(events, table), actions


def uinput_available() -> bool:
    return UINPUT_PATH.exists()


@dataclasses.dataclass
class _Channel:
    """One physical keyboard and the virtual device standing in for it."""

    path: Path
    source: Any
    virtual: Any = None
    grabbed: bool = False


class KeyboardRouter:
    """Drop-in for HotkeyListener that also remaps what it reads."""

    def __init__(
        self,
        devices: Sequence[Path],
        ptt_code: int,
        *,
        panic_code: int | None = None,
        ignore_repeat: bool = True,
        table: Mapping[int, int] | None = None,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        on_panic: Callable[[], None] | None = None,
        macro_codes: Mapping[int, Callable[[], None]] | None = None,
        rescan: Callable[[], list[Path]] | None = None,
    ) -> None:
        self.devices = [Path(d) for d in devices]
        self.ptt_code = ptt_code
        self.panic_code = panic_code
        self.ignore_repeat = ignore_repeat
        self.table = dict(table or {})
        self.on_press = on_press
        self.on_release = on_release
        self.on_panic = on_panic
# Same contract as HotkeyListener, so a macro bound to a key keeps
        # working whether or not a remap table happens to be active.
        self.macro_codes = dict(macro_codes or {})
        # Finds keyboards plugged in after startup, same as the plain listener.
        self._rescan = rescan

        self._channels: list[_Channel] = []
        self._down = False
        self._panic_down = False
        self._macro_down: set[int] = set()

    @property
    def watch(self) -> frozenset[int]:
        codes = {self.ptt_code}
        if self.panic_code is not None:
            codes.add(self.panic_code)
        return frozenset(codes)

    @property
    def remapping(self) -> bool:
        return bool(self.table)

    def open(self) -> None:
        """Take over every keyboard, or none of them.

        All or nothing on purpose. A half-remapped pair of keyboards is harder
        to reason about than either correct or untouched.
        """
        if not self.table:
            raise RuntimeError("router has nothing to remap")

        if not uinput_available():
            raise RuntimeError(f"{UINPUT_PATH} is missing; cannot remap")

        opened: list[_Channel] = []
        try:
            for path in self.devices:
                source = open_device(path)
                channel = _Channel(path=path, source=source, virtual=None)
                opened.append(channel)
                channel.virtual = self._create(source, path)
            for channel in opened:
                source = channel.source
                source.grab()
                channel.grabbed = True
        except Exception:
            for channel in opened:
                self._discard(channel)
            raise

        self._channels = opened
        log.info(
            "remapping %d device(s) through a virtual keyboard: %s",
            len(opened),
            ", ".join(c.path.name for c in opened),
        )

    def _create(self, source: Any, path: Path) -> Any:
        name = f"{VIRTUAL_NAME_PREFIX} keyboard {path.name}"
        return UInput(
            events=clone_capabilities(source.capabilities()),
            name=name,
            vendor=VIRTUAL_VENDOR,
            product=VIRTUAL_PRODUCT,
        )

    def _discard(self, channel: _Channel) -> None:
        if channel.grabbed:
            with contextlib.suppress(OSError):
                channel.source.ungrab()
            channel.grabbed = False
        if channel.virtual is not None:
            with contextlib.suppress(OSError):
                channel.virtual.close()
        with contextlib.suppress(OSError):
            channel.source.close()

    def close(self) -> None:
        for channel in self._channels:
            self._discard(channel)
        self._channels.clear()
        self._down = False
        self._panic_down = False

    def reset_state(self) -> None:
        self._down = False
        self._panic_down = False

    def _handle(self, action: KeyAction) -> None:
        if action.code == self.panic_code:
            if action.pressed and not self._panic_down:
                self._panic_down = True
                if self.on_panic:
                    self.on_panic()
            elif not action.pressed:
                self._panic_down = False
            return

        # PTT before macros, matching HotkeyListener: on a collision the
        # microphone keeps working rather than going silently dead.
        if action.code != self.ptt_code:
            if action.code in self.macro_codes:
                if action.pressed:
                    if action.code not in self._macro_down:
                        self._macro_down.add(action.code)
                        self.macro_codes[action.code]()
                else:
                    self._macro_down.discard(action.code)
            return

        if action.pressed:
            if not self._down:
                self._down = True
                if self.on_press:
                    self.on_press()
        else:
            if self._down:
                self._down = False
                if self.on_release:
                    self.on_release()

    def poll(self, timeout: float = 0.2) -> None:
        if not self._channels:
            raise RuntimeError("router is not open")

        watch_fds: dict[int, tuple[_Channel, bool]] = {}
        for channel in self._channels:
            watch_fds[channel.source.fd] = (channel, False)
            watch_fds[channel.virtual.fd] = (channel, True)

        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                readable, _, _ = select.select(list(watch_fds), [], [], remaining)
            except (OSError, ValueError):
                self._forget_dead()
                if not self._channels:
                    raise RuntimeError("all input devices disappeared") from None
                continue

            for fd in readable:
                channel, is_virtual = watch_fds[fd]
                if is_virtual:
                    self._forward_led(channel)
                else:
                    self._drain(channel)
            if time.monotonic() >= deadline:
                self._pick_up_new_devices()
                return

    def _pick_up_new_devices(self) -> None:
        """Add keyboards that appeared after the daemon started.

        All or nothing, like open(). A half-remapped pair is worse than either
        correct or untouched, so a keyboard that cannot be grabbed here is left
        alone and the ones already remapped carry on.
        """
        if self._rescan is None:
            return
        try:
            found = self._rescan()
        except Exception as exc:
            log.debug("cannot look for new keyboards: %s", exc)
            return
        wanted = [p for p in found if p not in {c.path for c in self._channels}]
        if not wanted:
            return

        added: list[_Channel] = []
        try:
            for path in wanted:
                source = open_device(path)
                channel = _Channel(path=path, source=source, virtual=None)
                added.append(channel)
                channel.virtual = self._create(source, path)
            for channel in added:
                channel.source.grab()
                channel.grabbed = True
        except Exception as exc:
            for channel in added:
                self._discard(channel)
            log.warning("cannot remap %s, leaving it unremapped: %s", wanted[0], exc)
            return

        self._channels.extend(added)
        log.info(
            "now remapping %d more device(s) through virtual keyboards: %s",
            len(added),
            ", ".join(c.path.name for c in added),
        )

    def _forward_led(self, channel: _Channel) -> None:
        """Send the desktop's key-light changes back to the real keyboard.

        Without this the light on the keyboard stops matching the lock state
        the desktop thinks it set, which is the sort of small wrongness people
        notice long before they notice anything else.
        """
        for event in channel.virtual.read():
            if event.type not in _OUTPUT_TYPES:
                continue
            with contextlib.suppress(OSError):
                channel.source.write(event.type, event.code, event.value)

    def _drain(self, channel: _Channel) -> None:
        try:
            events = channel.source.read()
        except OSError:
            log.warning("lost %s", channel.path)
            self._forget_dead()
            return

        batch = [(e.type, e.code, e.value) for e in events]
        forwarded, actions = plan(batch, self.table, self.watch, self.ignore_repeat)
        for action in actions:
            self._handle(action)
        if not forwarded:
            return
        for ev_type, code, value in forwarded:
            with contextlib.suppress(OSError):
                channel.virtual.write(ev_type, code, value)
        with contextlib.suppress(OSError):
            channel.virtual.syn()

    def _forget_dead(self) -> None:
        alive: list[_Channel] = []
        for channel in self._channels:
            if _fd_alive(channel.source):
                alive.append(channel)
            else:
                self._discard(channel)
        self._channels = alive
        if not self._channels and self._down:
            self._down = False
            if self.on_release:
                self.on_release()
