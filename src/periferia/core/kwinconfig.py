"""The pointer speed, as the compositor sees it.

A profile can ask for a different pointer speed, and there is exactly one place
that can answer: the compositor's own configuration file. Nothing here talks to
the mouse. A mouse's DPI is a property of the mouse; software can only scale
the pointer it draws, and that scale is what `speed` is.

So this writes `kwinrc` and asks KWin to reread it. That is deliberately the
compositor's file and not a private file of this project's: anything reading it
(the settings panel, kwin_wayland on start) sees the same value, and a
config written here does not disappear when this project stops running.

Everything here fails quietly and says why. Pointer speed is a convenience on
top of a program whose job is the microphone, and a session where KWin cannot be
asked should still push to talk.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

# kwinrc, group Mouse, key speed. KWin has kept this name across both the Qt5
# and Qt6 versions, which is the only reason this is a file edit rather than a
# D-Bus property.
GROUP = "Mouse"
KEY = "speed"
FILE = "kwinrc"

# What KWin treats as no scaling, and the range its own settings panel offers.
NEUTRAL = 1.0
MIN_SPEED = 0.05
MAX_SPEED = 10.0


def _tool(*names: str) -> str | None:
    """The first of these tools that exists.

    Checked in the order given so a session with both installed uses the newest,
    and a session with neither reports that instead of failing later.
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def read_tool() -> str | None:
    return _tool("kreadconfig6", "kreadconfig5")


def write_tool() -> str | None:
    return _tool("kwriteconfig6", "kwriteconfig5")


def available() -> bool:
    """Whether the pointer speed can be read and written on this machine."""
    return read_tool() is not None and write_tool() is not None


def _run(argv: list[str]) -> tuple[bool, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5.0, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if done.returncode != 0:
        return False, (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
    return True, done.stdout.strip()


def read_speed() -> float | None:
    """The speed KWin is using now, or None if it cannot be read.

    None is not the same as 1.0. A missing value means KWin's default, and
    writing that default back would put a key in the user's file that they never
    wrote.
    """
    tool = read_tool()
    if tool is None:
        return None
    ok, out = _run([tool, "--file", FILE, GROUP, KEY])
    if not ok:
        log.debug("cannot read the pointer speed: %s", out)
        return None
    written = out.strip()
    if not written:
        return None
    try:
        value = float(written)
    except ValueError:
        log.debug("pointer speed in %s is %r, which is not a number", FILE, written)
        return None
    return value


def write_speed(speed: float) -> bool:
    """Ask KWin to use this pointer speed. False with the reason logged.

    Writing the file is only half of it: KWin reads kwinrc when it starts and
    when something tells it to reread, so without the reconfigure call the
    value would sit in the file doing nothing until the next login.
    """
    tool = write_tool()
    if tool is None:
        log.debug("cannot write the pointer speed: kwriteconfig is not installed")
        return False

    ok, detail = _run([tool, "--file", FILE, GROUP, KEY, f"{speed:g}"])
    if not ok:
        log.warning("cannot write the pointer speed: %s", detail)
        return False

    return reconfigure()


def reconfigure() -> bool:
    """Tell KWin to reread its configuration.

    Both the Qt5 and Qt6 interface names are tried because there is no version
    test worth having here: whichever one answers is the one running.
    """
    for dbus_tool in ("qdbus6", "qdbus"):
        tool = shutil.which(dbus_tool)
        if tool is None:
            continue
        ok, detail = _run([tool, "org.kde.KWin", "/KWin", "reconfigure"])
        if ok:
            return True
        log.debug("%s reconfigure failed: %s", dbus_tool, detail)
    log.warning(
        "the pointer speed is written to %s but KWin was not asked to reread it; "
        "it will apply at the next login",
        FILE,
    )
    return False


def apply(speed: float | None, restore: float | None) -> bool:
    """Set the pointer speed, or put back what was there.

    `restore` is the value this project found before it touched anything. It is
    written when a profile in force has no speed of its own, so a config that
    only speeds the pointer up in one window does not leave the pointer fast in
    every other one after the daemon stops.

    A profile that only ever moves the pointer in one direction, with no
    fallback profile to restore to, has nothing to restore and says so once
    rather than guessing.
    """
    if speed is not None:
        return write_speed(speed)
    if restore is None:
        return False
    return write_speed(restore)