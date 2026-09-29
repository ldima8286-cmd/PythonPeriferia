"""Keyboard capture over evdev.

Reads raw key events from /dev/input/eventX. Values are physical keycodes, so
the current keyboard layout is irrelevant: KEY_47 is the physical V key, which
prints "M" on a Russian layout and "V" on an English one.
"""

from __future__ import annotations

import contextlib
import logging
import select
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from evdev import InputDevice, ecodes
except ImportError:  # pragma: no cover
    InputDevice = None  # type: ignore[assignment,misc]
    ecodes = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

BY_ID = Path("/dev/input/by-id")

PRESS = 1
RELEASE = 0
REPEAT = 2

# Physical keycode -> the letter each layout puts there, for display only.
RU_LETTERS = {
    "KEY_Q": "Й", "KEY_W": "Ц", "KEY_E": "У", "KEY_R": "К", "KEY_T": "Е",
    "KEY_Y": "Н", "KEY_U": "Г", "KEY_I": "Ш", "KEY_O": "Щ", "KEY_P": "З",
    "KEY_A": "Ф", "KEY_S": "Ы", "KEY_D": "В", "KEY_F": "А", "KEY_G": "П",
    "KEY_H": "Р", "KEY_J": "О", "KEY_K": "Л", "KEY_L": "Д",
    "KEY_Z": "Я", "KEY_X": "Ч", "KEY_C": "С", "KEY_V": "М", "KEY_B": "И",
    "KEY_N": "Т", "KEY_M": "Ь", "KEY_GRAVE": "Ё",
}

RU_TO_KEYCODE = {letter: code for code, letter in RU_LETTERS.items()}


def resolve_key(name: str) -> int | None:
    """Accept a KEY_* name, a single Russian letter, or an English letter."""
    if ecodes is None:
        return None
    name = name.strip()

    if len(name) == 1 and name.isalpha():
        upper = name.upper()
        code = RU_TO_KEYCODE.get(upper)
        if code is None:
            code = RU_TO_KEYCODE.get(name)
        if code is None:
            code = f"KEY_{upper}"
        name = code

    if not name.startswith("KEY_") and not name.startswith("BTN_"):
        name = f"KEY_{name}"

    return ecodes.ecodes.get(name)


def key_label(code: int) -> str:
    if ecodes is None:
        return str(code)
    name = ecodes.KEY[code]
    if isinstance(name, tuple):
        name = name[0]
    letter = RU_LETTERS.get(name)
    if letter:
        return f"{letter} ({name.removeprefix('KEY_')})"
    return name.removeprefix("KEY_")


def list_input_devices() -> list[tuple[Path, str]]:
    """Return (event node, readable name) for every keyboard-ish device."""
    if not BY_ID.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for link in sorted(BY_ID.iterdir()):
        name = link.name
        if "-event-kbd" in name or "keyboard" in name.lower() or "-event-if" in name:
            try:
                target = link.resolve()
            except OSError:
                continue
            if target in seen or not target.exists():
                continue
            seen.add(target)
            found.append((target, name))
    return found


def open_device(path: Path | str) -> Any:
    """Open an input device for reading.

    evdev has no logind/libseat support in any released version, so there is no
    way to borrow the desktop's access: the process itself needs permission on
    the node. `readonly=True` avoids the O_RDWR attempt, which can poke device
    firmware state for no benefit when we only read key events.
    """
    return InputDevice(str(path), readonly=True)


def pick_device(preferred: str = "auto") -> Path | None:
    if preferred != "auto":
        path = Path(preferred)
        if path.exists():
            return path
        log.warning("configured device %s not found, falling back to auto", preferred)

    if InputDevice is None:
        return None
    for target, _name in list_input_devices():
        try:
            dev = open_device(target)
        except OSError as exc:
            log.debug("cannot open %s: %s", target, exc)
            continue
        names = {ecodes.KEY[n] for n in dev.capabilities().get(ecodes.EV_KEY, [])}
        is_kbd = "KEY_ENTER" in names or "KEY_SPACE" in names
        dev.close()
        if is_kbd:
            return target
    return None


class HotkeyListener:
    """Blocking event loop that calls back on press and release."""

    def __init__(
        self,
        device: Path,
        ptt_code: int,
        *,
        panic_code: int | None = None,
        ignore_repeat: bool = True,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        on_panic: Callable[[], None] | None = None,
    ) -> None:
        if InputDevice is None:
            raise RuntimeError("evdev is not installed")
        self.device = device
        self.ptt_code = ptt_code
        self.panic_code = panic_code
        self.ignore_repeat = ignore_repeat
        self.on_press = on_press
        self.on_release = on_release
        self.on_panic = on_panic
        self._dev: InputDevice | None = None
        self._down = False

    def open(self) -> None:
        self._dev = open_device(self.device)
        log.info("listening on %s", self.device)

    def close(self) -> None:
        if self._dev is not None:
            with contextlib.suppress(OSError):
                self._dev.close()
            self._dev = None

    def poll(self, timeout: float = 0.2) -> None:
        if self._dev is None:
            raise RuntimeError("listener is not open")
        try:
            readable, _, _ = select.select([self._dev.fd], [], [], timeout)
        except (OSError, ValueError) as exc:
            log.error("input device disappeared: %s", exc)
            raise
        if not readable:
            return
        for event in self._dev.read():
            self._handle(event.type, event.code, event.value)

    def _handle(self, ev_type: int, code: int, value: int) -> None:
        if ev_type != ecodes.EV_KEY:
            return
        if self.ignore_repeat and value == REPEAT:
            return

        if self.panic_code is not None and code == self.panic_code and value == PRESS:
            log.warning("panic")
            if self.on_panic:
                self.on_panic()
            return

        if code != self.ptt_code:
            return

        if value == PRESS and not self._down:
            self._down = True
            if self.on_press:
                self.on_press()
        elif value == RELEASE and self._down:
            self._down = False
            if self.on_release:
                self.on_release()
