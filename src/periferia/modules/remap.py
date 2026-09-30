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


def held_modifier_codes() -> frozenset[int]:
    """Modifiers that stay pressed while held.

    Lock keys such as CAPSLOCK are deliberately absent: they toggle rather than
    hold, so their state is visible on the keyboard light, and remapping one is
    both safe and a common thing to want.
    """
    return frozenset(
        code for code in (resolve_key(name) for name in HELD_MODIFIER_NAMES) if code is not None
    )


def build_remap(pairs: Mapping[str, str]) -> dict[int, int]:
    """Turn a table of key names into a table of codes, or refuse it.

    Held modifiers are refused outright rather than half supported. Emitting one
    onto a virtual device means keeping a shadow copy of the desktop's modifier
    state in step with the real one, and any gap in that bookkeeping leaves
    shift held down until the next reboot. Lock keys and ordinary keys have no
    such state, so they are safe to remap.
    """
    if not pairs:
        return {}

    modifiers = held_modifier_codes()
    table: dict[int, int] = {}
    seen: dict[int, str] = {}

    for source_name, target_name in pairs.items():
        source = resolve_key(str(source_name))
        if source is None:
            raise RemapError(f"unknown source key: {source_name!r}")

        target = resolve_key(str(target_name))
        if target is None:
            raise RemapError(f"{source_name} maps to unknown target key: {target_name!r}")

        if source == target:
            raise RemapError(f"{source_name} maps to itself, which changes nothing")

        if source in seen:
            raise RemapError(f"{source_name} is mapped twice in the same profile")
        seen[source] = str(target_name)

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


def translate_key(code: int, table: Mapping[int, int]) -> int:
    return table.get(code, code)


def translate_events(
    events: Iterable[tuple[int, int, int]], table: Mapping[int, int]
) -> list[tuple[int, int, int]]:
    """Rewrite the key code in each (type, code, value) event, passing the rest.

    Only EV_KEY is touched. Synch, repeat and anything else are forwarded
    untouched, since a remap changes which key is pressed, not how events are
    framed.
    """
    out: list[tuple[int, int, int]] = []
    for ev_type, code, value in events:
        if ev_type == ecodes.EV_KEY and code in table:
            out.append((ev_type, table[code], value))
        else:
            out.append((ev_type, code, value))
    return out


def active_remap(profiles: Iterable[Any]) -> dict[int, int] | None:
    """Build the table for the profile in effect, or None to leave keys alone.

    Only the first enabled profile is used. Selecting by focused window is the
    part that needs a Wayland portal, and a list of profiles that silently
    always means the first one is worse than a single profile named clearly.

    Raises RemapError on a table that cannot be applied. The caller decides
    whether that is fatal; a broken table should be reported, not obeyed.
    """
    for entry in profiles:
        if not getattr(entry, "enabled", True):
            continue
        pairs = getattr(entry, "remap", None) or {}
        if not pairs:
            continue
        return build_remap(pairs)
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
