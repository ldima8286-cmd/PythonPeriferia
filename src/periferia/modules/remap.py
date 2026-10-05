"""Keyboard profiles: turn one physical key into a different key.

A profile is a table of source key to target key. Tables are validated hard at
startup, because a broken table does not announce itself. A swallowed key or a
stuck modifier just quietly gives you a keyboard that misbehaves, with nothing
to read and nothing to grep.

This module builds and checks tables only. Actually diverting a keyboard through
uinput means taking exclusive ownership of the device, and a grabbed device
stops delivering events to every other reader, so that layer is separate and
opt in.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import Any

from evdev import ecodes

from .hotkey import key_label, resolve_key

HELD_MODIFIER_NAMES = frozenset(
    {
        "KEY_LEFTSHIFT",
        "KEY_RIGHTSHIFT",
        "KEY_LEFTCTRL",
        "KEY_RIGHTCTRL",
        "KEY_LEFTALT",
        "KEY_RIGHTALT",
        "KEY_LEFTMETA",
        "KEY_RIGHTMETA",
    }
)


class RemapError(ValueError):
    """A remap table that cannot be applied safely."""


# What a target may be written as when the key should not exist at all. Read
# before any key name so "none" is never mistaken for one.
DISABLED_TARGETS = frozenset({"none", "off", "disable", "disabled", "-"})

# A remap maps a code to the code to emit instead, or to None for a key that
# emits nothing at all. None is the point of the type: "disabled" and "mapped to
# something" are both things a profile can say about a key, and a second
# parallel dict would let them disagree.
RemapTable = dict[int, "int | None"]


def held_modifier_codes() -> frozenset[int]:
    """Modifiers that stay pressed while held.

    Lock keys such as CAPSLOCK are deliberately absent: they toggle rather than
    hold, so their state is visible on the keyboard light, and remapping one is
    both safe and a common thing to want.
    """
    return frozenset(
        code for code in (resolve_key(name) for name in HELD_MODIFIER_NAMES) if code is not None
    )


def build_remap(pairs: Mapping[str, str]) -> dict[int, int | None]:
    """Turn a table of key names into a table of codes, or refuse it.

    A target of `none` (also `off`, `disable`, `-`) disables the key: nothing is
    emitted for it at all. That is the one part of this table with no way to
    notice it went wrong, so the key has to be named for a reason and the log
    says which ones went dead.

    Held modifiers are refused outright rather than half supported. Emitting one
    onto a virtual device means keeping a shadow copy of the desktop's modifier
    state in step with the real one, and any gap in that bookkeeping leaves
    shift held down until the next reboot. Lock keys and ordinary keys have no
    such state, so they are safe to remap.
    """
    if not pairs:
        return {}

    modifiers = held_modifier_codes()
    table: dict[int, int | None] = {}
    seen: dict[int, str] = {}

    for source_name, target_name in pairs.items():
        source = resolve_key(str(source_name))
        if source is None:
            raise RemapError(f"unknown source key: {source_name!r}")

        written = str(target_name).strip()
        if written.lower() in DISABLED_TARGETS:
            # A modifier left out is the one case where disabling is safe: it
            # emits nothing, so it cannot be left held down on the virtual
            # device either.
            table[source] = None
            seen[source] = written
            continue

        target = resolve_key(written)
        if target is None:
            raise RemapError(f"{source_name} maps to unknown target key: {target_name!r}")

        if source == target:
            raise RemapError(f"{source_name} maps to itself, which changes nothing")

        if source in seen:
            raise RemapError(f"{source_name} is mapped twice in the same profile")
        seen[source] = written

        if source in modifiers:
            raise RemapError(
                f"{source_name} is a held modifier and cannot be remapped yet: a virtual "
                "device that emits a held modifier can leave it stuck down"
            )
        if target in modifiers:
            raise RemapError(
                f"{source_name} maps to {target_name}, a modifier, which can leave "
                f"{target_name} stuck down"
            )

        table[source] = target

    return table


def translate_key(code: int, table: Mapping[int, int | None]) -> int | None:
    """The code to emit for a physical key, or None if the profile kills it."""
    return table.get(code, code)


def translate_events(
    events: Iterable[tuple[int, int, int]], table: Mapping[int, int | None]
) -> list[tuple[int, int, int]]:
    """Rewrite the key code in each (type, code, value) event, passing the rest.

    Only EV_KEY is touched. Synch, repeat and anything else are forwarded
    untouched, since a remap changes which key is pressed, not how events are
    framed.

    A key the table kills is left out entirely rather than emitted as something
    harmless. Dropping it is the whole meaning of "disabled": the application
    never hears of it, so it cannot be triggered by a macro, a game that reads
    the key directly, or a held key that was already down when the profile
    changed.
    """
    out: list[tuple[int, int, int]] = []
    for ev_type, code, value in events:
        if ev_type == ecodes.EV_KEY and code in table:
            target = table[code]
            if target is None:
                continue
            out.append((ev_type, target, value))
        else:
            out.append((ev_type, code, value))
    return out


def remap_for(profile: Any) -> dict[int, int | None] | None:
    """Build the table for one named profile, or None if it remaps nothing.

    Separate from active_remap() because selecting the profile is not the same
    problem as building the table. The table is the same either way; only the
    question of *which* profile changes once something can see the focused
    window.

    Raises RemapError on a table that cannot be applied, same as active_remap.
    """
    pairs = getattr(profile, "remap", None) or {}
    if not pairs:
        return None
    return build_remap(pairs)


def active_remap(profiles: Iterable[Any]) -> dict[int, int | None] | None:
    """Build the table for the profile in effect, or None to leave keys alone.

    Only the first enabled profile is used. Selecting by focused window is
    handled above this, in windowprofile and the watcher that feeds it; this is
    the fallback for a compositor that cannot say what has focus, and for a
    profile that names no window.

    Raises RemapError on a table that cannot be applied. The caller decides
    whether that is fatal; a broken table should be reported, not obeyed.
    """
    for entry in profiles:
        if not getattr(entry, "enabled", True):
            continue
        table = remap_for(entry)
        if table:
            return table
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class Profile:
    """One named remap table, ready to apply."""

    name: str
    table: Mapping[int, int]

    def __len__(self) -> int:
        return len(self.table)

    def conflicts_with(self, *codes: int) -> list[int]:
        """Source keys in this table that the caller also listens for."""
        return sorted(code for code in codes if code in self.table)

    def describe(self) -> str:
        if not self.table:
            return f"{self.name}: no remapping"
        pairs = ", ".join(
            f"{key_label(source)} -> {key_label(target)}"
            for source, target in sorted(self.table.items())
        )
        return f"{self.name}: {pairs}"
