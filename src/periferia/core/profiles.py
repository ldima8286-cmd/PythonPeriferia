"""Move profiles between machines.

A tuned profile set is the one thing a machine cannot remember for another
machine: it is long, it is tuned to a set of games, and retyping it on a
second computer is exactly the kind of task that fails at 2 a.m. Export
writes the profiles as a small YAML file, import reads it back, validates it
against the same rules the config itself is read by, and merges it into the
config that is in use.

The file is written as a `profiles:` block so it looks like the slice of the
config it is. Import also accepts a bare list, because that is what the
finished block reads like once the leading key is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import config as config_mod
from .config import Config, ProfileConfig


def export_block(conf: Config) -> dict[str, Any]:
    """The profiles as a standalone YAML container.

    Only the profiles leave: everything else a config carries belongs to the
    machine it is on, and a key layout travels worse than a remap.
    """
    return {"profiles": [config_mod._unbox(p) for p in conf.profiles]}


def parse_text(text: str) -> list[ProfileConfig]:
    """Turn an exported block (or a bare list) into validated profiles.

    The same checks the config goes through apply here, so a file that would
    break the daemon is refused before it is written anywhere.
    """
    data = yaml.safe_load(text)
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        data = data["profiles"]
    return config_mod._build_profiles(data)


def parse_file(path: str | Path) -> list[ProfileConfig]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise
    return parse_text(text)


def merge(
    conf: Config,
    incoming: list[ProfileConfig],
    *,
    replace: bool = False,
) -> Config:
    """Put `incoming` into the config without losing the rest.

    Without `replace` an imported profile with a name that already exists
    takes that profile's place and profiles nobody asked for are kept. Order
    matters to the router, so existing positions are preserved and new
    profiles go at the end: a fallback that moved to the front would start
    claiming every window.
    """
    if replace:
        conf.profiles = incoming
        return conf

    fresh = {p.name: p for p in incoming}
    merged: list[ProfileConfig] = []
    seen: set[str] = set()
    for profile in conf.profiles:
        if profile.name in fresh:
            merged.append(fresh.pop(profile.name))
            seen.add(profile.name)
        else:
            merged.append(profile)
    merged += [p for p in incoming if p.name not in seen]
    conf.profiles = merged
    return conf


def read_raw(path: str | Path) -> dict[str, Any]:
    """Read a config file the way the CLI always has: a plain mapping.

    The sections the import does not touch are read and written back so that
    importing profiles cannot drop the rest of a machine's config.
    """
    text = Path(path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: the config must be a mapping")
    return parsed


def write_raw(path: str | Path, container: dict[str, Any]) -> None:
    """Write a config container back as plain YAML, in writing order."""
    Path(path).write_text(
        yaml.safe_dump(container, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )