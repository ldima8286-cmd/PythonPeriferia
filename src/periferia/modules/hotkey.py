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
BY_PATH = Path("/dev/input/by-path")
INPUT = Path("/dev/input")


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
    name = code_name(code)
    letter = RU_LETTERS.get(name)
    if letter:
        return f"{letter} ({name.removeprefix('KEY_')})"
    return name.removeprefix("KEY_")


VIRTUAL_NAME_PREFIX = "Periferia virtual"


def is_virtual_device(name: str) -> bool:
    """True for a virtual keyboard this program made.

    Discovery has to recognise these. A leftover virtual device from a previous
    run still advertises every key the program can press, so it passes for a
    real keyboard, and grabbing it would stack a new virtual device on the old
    one and feed the output back into its own input.
    """
    return name.startswith(VIRTUAL_NAME_PREFIX)


def list_input_devices() -> list[tuple[Path, str]]:
    """Return (event node, readable name) for every input device we could use.

    /dev/input/by-id alone is not enough. A USB keyboard without a serial number
    gets no by-id symlink at all, so scanning only that directory silently drops
    it: the daemon then listens to the laptop keyboard and the external one
    looks broken, with no error anywhere to explain why. by-path and the bare
    event nodes are scanned as well, and what each node really is gets decided
    by opening it, not by guessing from its file name.
    """
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(target: Path, name: str) -> None:
        if target in seen or not target.exists():
            return
        seen.add(target)
        found.append((target, name))

    for directory, wanted in ((BY_ID, True), (BY_PATH, False)):
        if not directory.is_dir():
            continue
        for link in sorted(directory.iterdir()):
            name = link.name
            if wanted and not (
                "-event-kbd" in name or "keyboard" in name.lower() or "-event-if" in name
            ):
                continue
            try:
                add(link.resolve(), name)
            except OSError:
                continue

    if INPUT.is_dir():
        for node in sorted(INPUT.glob("event*")):
            add(node, node.name)
    return found


def open_device(path: Path | str) -> Any:
    """Open an input device for reading.

    evdev has no logind/libseat support in any released version, so there is no
    way to borrow the desktop's access: the process itself needs permission on
    the node. `readonly=True` avoids the O_RDWR attempt, which can poke device
    firmware state for no benefit when we only read key events.
    """
    return InputDevice(str(path), readonly=True)


def _first_name(name: str | tuple[str, ...] | None) -> str:
    """evdev returns a tuple of aliases for some codes, e.g. KEY_HANGEUL."""
    if name is None:
        return ""
    if isinstance(name, tuple):
        return name[0] if name else ""
    return name


def _build_code_to_name() -> dict[int, str]:
    """Every code to its first name, keys and buttons alike.

    ecodes.KEY only covers typing keys and holds no mouse buttons at all, so
    looking a BTN_ code up there returns nothing. ecodes.ecodes maps names to
    codes for both, so it is inverted to get the reverse lookup.
    """
    names: dict[int, list[str]] = {}
    if ecodes is None:
        return {}
    for name, value in ecodes.ecodes.items():
        codes = value if isinstance(value, tuple) else (value,)
        for code in codes:
            if isinstance(code, int):
                names.setdefault(code, []).append(_first_name(name))

    out: dict[int, str] = {}
    for code, candidates in names.items():
        # a code can be spelled several ways, KEY_F12 also being FF_SQUARE.
        # The KEY_/BTN_ spelling is the one users see on a keycap.
        preferred = [n for n in candidates if n.startswith(("KEY_", "BTN_"))]
        out[code] = (preferred or candidates)[0]
    return out


CODE_TO_NAME = _build_code_to_name()


def code_name(code: int) -> str:
    """Name of a key or button code, never None."""
    return CODE_TO_NAME.get(code) or f"code {code}"


def is_button_code(code: int) -> bool:
    """True for mouse buttons rather than typing keys."""
    return code_name(code).startswith("BTN_")


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
    names = {code_name(code) for code in codes}
    return "KEY_ENTER" in names and "KEY_SPACE" in names


def is_pointer(dev: Any) -> bool:
    """True for a device with mouse buttons.

    Mice are included so a side button can drive PTT, which the design calls
    for. Only EV_KEY is ever read from them, so movement events are ignored
    by the event loop and watching one costs nothing but a file descriptor.
    """
    try:
        caps = dev.capabilities()
    except Exception:
        return False
    names = {code_name(code) for code in caps.get(ecodes.EV_KEY, [])}
    has_button = any(n.startswith("BTN_") for n in names)
    has_motion = bool(caps.get(ecodes.EV_REL)) or bool(caps.get(ecodes.EV_ABS))
    return has_button and has_motion


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


def device_group(dev: Any, name: str, path: Path) -> str:
    """One key per physical device, so a keyboard is not watched twice.

    evdev reports the physical path of the device. Its HID and boot protocol
    nodes share everything up to the trailing inputN, which is exactly the part
    that says "same keyboard", and unlike a file name it is available even when
    the device has no by-id symlink to name it by.
    """
    phys = str(getattr(dev, "phys", "") or "").strip()
    if phys:
        return re.sub(r"/input\d+.*$", "", phys)
    return physical_device_id(name) or str(path)


def find_keyboards(
    preferred: str = "auto", *, include_pointers: bool = False
) -> list[Path]:
    """Every usable keyboard, not just the first one.

    A machine can easily have three: the laptop keyboard, an external USB one
    and a Bluetooth one. Listening to a single device means the PTT key works
    only on that device, which looks like a broken key elsewhere. "auto"
    returns all of them. An explicit value may list several, comma separated.

    Pointing devices are included when `include_pointers` is set, which the
    daemon does only when the PTT key is a mouse button. Reading a mouse needs
    an extra udev rule, so nobody should pay for it until they ask for it.
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
        try:
            dev = open_device(target)
        except OSError as exc:
            log.debug("cannot open %s: %s", target, exc)
            continue
        try:
            ours = is_virtual_device(getattr(dev, "name", "") or "")
            usable = is_keyboard(dev) or (include_pointers and is_pointer(dev))
        finally:
            with contextlib.suppress(OSError):
                dev.close()
        if ours:
            log.debug("skipping %s: it is a virtual device of ours", target)
            continue
        if not usable:
            continue
        # Every usable node is watched. Picking one node per physical device
        # sounds tidier, but there is no way to tell which interface actually
        # carries the keys: on one external keyboard here, input0 and input1
        # reported key capabilities and stayed silent, while input2 delivered
        # every press. Choosing the first one made PTT dead with no error, and
        # the press state guards make a duplicate harmless anyway.
        group = device_group(dev, name, target)
        if group in seen:
            log.info(
                "%s is another interface of a device already watched, keeping it: "
                "it may be the one that delivers the keys",
                target,
            )
        seen.add(group)
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

    def reset_state(self) -> None:
        """Forget that a key is held, so the next press is not a duplicate."""
        self._down = False
        self._panic_down = False

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
