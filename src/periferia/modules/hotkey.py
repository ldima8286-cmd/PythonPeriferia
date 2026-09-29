"""Keyboard capture over evdev.

Reads raw key events from /dev/input/eventX. Values are physical keycodes, so
the current keyboard layout is irrelevant: KEY_47 is the physical V key, which
prints "M" on a Russian layout and "V" on an English one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import select
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

try:
    from evdev import InputDevice, ecodes
except ImportError:  # pragma: no cover
    InputDevice = None  # type: ignore[assignment,misc]
    ecodes = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

BY_ID = Path("/dev/input/by-id")


def _fd_alive(dev: Any) -> bool:
    try:
        os.fstat(dev.fd)
    except (OSError, ValueError):
        return False
    return True

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


def is_keyboard(dev: Any) -> bool:
    """True when the device emits ordinary typing keys.

    Capabilities are the only reliable test. The name in /dev/input/by-id is
    whatever the vendor typed in, and a mouse exposes a keyboard style
    interface too, which is why filtering on "event-kbd" alone is not enough.
    """
    try:
        codes = dev.capabilities().get(ecodes.EV_KEY, [])
    except Exception:
        return False
    names = {ecodes.KEY.get(code) for code in codes}
    return "KEY_ENTER" in names and "KEY_SPACE" in names


def physical_device_id(by_id_name: str) -> str:
    """Strip the interface suffix from a /dev/input/by-id name.

    One physical keyboard normally shows up as several nodes: the HID
    interface plus the boot protocol one, named `-event-kbd` and
    `-if02-event-kbd`. Both deliver the same key presses, so watching both
    means every keystroke arrives twice. Grouping by the part before the
    suffix keeps one node per real device.
    """
    name = re.sub(r"-if\d+-event-kbd$", "", by_id_name)
    name = re.sub(r"-event-kbd$", "", name)
    name = re.sub(r"-event-if\d+$", "", name)
    return name


def find_keyboards(preferred: str = "auto") -> list[Path]:
    """Every usable keyboard, not just the first one.

    A machine can easily have three: the laptop keyboard, an external USB one
    and a Bluetooth one. Listening to a single device means the PTT key works
    only on that device, which looks like a broken key elsewhere. "auto"
    returns all of them. An explicit value may list several, comma separated.
    """
    if InputDevice is None:
        return []

    if preferred and preferred != "auto":
        wanted: list[Path] = []
        for part in preferred.split(","):
            part = part.strip()
            if not part:
                continue
            path = Path(part)
            if not path.exists():
                log.warning("configured device %s not found", path)
                continue
            wanted.append(path)
        if wanted:
            return wanted
        return []

    found: list[Path] = []
    seen: set[str] = set()
    for target, name in list_input_devices():
        device_id = physical_device_id(name)
        if device_id in seen:
            log.debug("skipping %s, same device as an already accepted node", target)
            continue
        try:
            dev = open_device(target)
        except OSError as exc:
            log.debug("cannot open %s: %s", target, exc)
            continue
        try:
            if not is_keyboard(dev):
                continue
        finally:
            with contextlib.suppress(OSError):
                dev.close()
        seen.add(device_id)
        found.append(target)
    return found


def pick_device(preferred: str = "auto") -> Path | None:
    """Backwards compatible single device accessor."""
    devices = find_keyboards(preferred)
    return devices[0] if devices else None


class HotkeyListener:
    """Watches every keyboard at once and calls back on press and release.

    Press state is global rather than per device, so pressing PTT on the laptop
    keyboard and releasing it on the external one still closes the microphone
    instead of leaving it stuck open.
    """

    def __init__(
        self,
        devices: Sequence[Path],
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
        if isinstance(devices, (str, Path)):
            devices = [Path(devices)]
        self.devices = [Path(d) for d in devices]
        self.ptt_code = ptt_code
        self.panic_code = panic_code
        self.ignore_repeat = ignore_repeat
        self.on_press = on_press
        self.on_release = on_release
        self.on_panic = on_panic
        self._devs: dict[Path, Any] = {}
        self._down = False
        self._panic_down = False

    def open(self) -> None:
        for path in self.devices:
            self._devs[path] = open_device(path)
        log.info(
            "listening on %d device(s): %s",
            len(self._devs),
            ", ".join(str(p) for p in self._devs),
        )

    def close(self) -> None:
        for dev in self._devs.values():
            with contextlib.suppress(OSError):
                dev.close()
        self._devs.clear()

    def _drop(self, path: Path, reason: str) -> None:
        dev = self._devs.pop(path, None)
        if dev is not None:
            with contextlib.suppress(OSError):
                dev.close()
        log.warning("stopped watching %s: %s", path, reason)
        if self._down:
            self._down = False
            if self.on_release:
                self.on_release()

    def poll(self, timeout: float = 0.2) -> None:
        if not self._devs:
            raise RuntimeError("listener is not open")
        deadline = time.monotonic() + timeout
        while True:
            by_fd = {dev.fd: path for path, dev in self._devs.items()}
            remaining = max(0.0, deadline - time.monotonic())
            try:
                readable, _, _ = select.select(list(by_fd), [], [], remaining)
            except (OSError, ValueError):
                # A device can vanish between select() calls when a USB keyboard
                # is unplugged. Drop the offender and retry on the rest, so that
                # events already queued on the surviving keyboards are not
                # swallowed by somebody else's disappearance.
                gone = [path for path, dev in self._devs.items() if not _fd_alive(dev)]
                for path in gone:
                    self._drop(path, "device disappeared")
                if not self._devs:
                    raise RuntimeError("all input devices disappeared") from None
                continue
            for fd in readable:
                path = by_fd[fd]
                dev = self._devs[path]
                try:
                    events = list(dev.read())
                except OSError as exc:
                    self._drop(path, str(exc))
                    continue
                for event in events:
                    self._handle(event.type, event.code, event.value)
            return

    def _handle(self, ev_type: int, code: int, value: int) -> None:
        if ev_type != ecodes.EV_KEY:
            return
        if self.ignore_repeat and value == REPEAT:
            return

        if self.panic_code is not None and code == self.panic_code and value == PRESS:
            # guarded like PTT: the same physical key can arrive from two
            # interfaces of one keyboard, and a second panic would be noise
            if not self._panic_down:
                self._panic_down = True
                log.warning("panic")
                if self.on_panic:
                    self.on_panic()
            return
        if self.panic_code is not None and code == self.panic_code and value == RELEASE:
            self._panic_down = False
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
