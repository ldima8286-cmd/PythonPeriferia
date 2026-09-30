"""Everything the window needs to know, with no Qt in sight.

The widgets stay thin on purpose. Editing a remap table, refusing an illegal
mapping and saving a config file without destroying the comments somebody wrote
in it are all decisions worth testing, and none of them need a display.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..core import state as state_mod
from ..modules.hotkey import key_label, resolve_key
from ..modules.remap import RemapError, build_remap

try:
    from ruamel.yaml import YAML

    HAVE_ROUND_TRIP = True
except ImportError:  # pragma: no cover - exercised only without the extra
    HAVE_ROUND_TRIP = False


def _yaml_for(text: str) -> Any:
    """A YAML dumper that keeps the indentation style the file already uses.

    ruamel defaults to writing sequences flush at column zero, so loading a
    config that indents its lists and dumping it again silently reflows the
    whole file. The style is read from the text rather than assumed, because
    both styles are common and rewriting somebody else's formatting on every
    save is the sort of thing that makes a settings GUI unwelcome.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    offset = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- "):
            offset = len(line) - len(stripped)
            break
    yaml.indent(mapping=2, sequence=max(offset + 2, 2), offset=offset)
    return yaml


@dataclasses.dataclass(frozen=True, slots=True)
class KeyChoice:
    """A key as the user reads it, and as the config has to spell it.

    `label` is for the picker and `name` for the file. They are different
    things: a Russian user looks for Ё, where the keycode is called GRAVE.
    Rebuilding the name from the label is how that pair stops matching, so the
    name is carried alongside instead of being re-derived.
    """

    code: int
    name: str
    label: str


def available_keys() -> list[KeyChoice]:
    """Every key the picker offers, in a stable order.

    Buttons are excluded: they are a different kind of thing and the remapper
    only promises to handle keys. `KEY_RESERVED` and the count macros are
    dropped for the same reason.

    Sorted by name rather than label. The labels carry Russian letters, and
    sorting on those would put the whole alphabet in the middle of the list
    with the digits on either side, which is not a list anybody can read.
    """
    from evdev import ecodes

    out: list[KeyChoice] = []
    for name, code in ecodes.ecodes.items():
        if not name.startswith("KEY_"):
            continue
        if name in ("KEY_RESERVED", "KEY_MAX", "KEY_CNT"):
            continue
        out.append(KeyChoice(code=code, name=name, label=key_label(code)))
    out.sort(key=lambda k: k.name)
    return out


Row = tuple[str, str]


def rows_from_profiles(profiles: Iterable[Any]) -> list[Row]:
    """Flatten the configured profiles into editable rows.

    Only the profile in effect is editable. A second profile cannot be selected
    yet, so showing its table in an editor that cannot choose it would invite
    somebody to change something that never applies.
    """
    rows: list[Row] = []
    for entry in profiles:
        if not getattr(entry, "enabled", True):
            continue
        remap = getattr(entry, "remap", None) or {}
        if not remap:
            continue
        rows.extend((str(k), str(v)) for k, v in remap.items())
        break
    return rows


def profile_name(profiles: Iterable[Any]) -> str:
    for entry in profiles:
        if getattr(entry, "enabled", True) and (getattr(entry, "remap", None) or {}):
            return str(getattr(entry, "name", "default"))
    return "default"


def add_row(rows: Sequence[Row], source: str, target: str) -> list[Row]:
    """Append a mapping, refusing the ones the router would refuse.

    The check is the same one the router makes, so the window cannot save
    something that would only fail later with the keyboard already grabbed.
    """
    candidate = dict(rows)
    candidate[source] = target
    build_remap(candidate)
    return [*rows, (source, target)]


def suggest_row(rows: Sequence[Row], keys: Sequence[KeyChoice]) -> Row | None:
    """Two unused keys to start a new mapping with, or None if there are none.

    The Add button used to copy the last row, which produced a duplicate the
    moment anything was already mapped. A new row should always be something
    the user can accept as is, and two free distinct keys always are.
    """
    used = {row[0] for row in rows} | {row[1] for row in rows}
    free = [choice.name for choice in keys if choice.name not in used]
    if len(free) < 2:
        return None
    return (free[0], free[1])


def remove_row(rows: Sequence[Row], index: int) -> list[Row]:
    if not 0 <= index < len(rows):
        raise IndexError(index)
    return [row for i, row in enumerate(rows) if i != index]


def rows_to_mapping(rows: Sequence[Row]) -> dict[str, str]:
    out: dict[str, str] = {}
    for source, target in rows:
        if source in out:
            raise RemapError(f"{source} is mapped twice in the same profile")
        out[source] = target
    return out


def rows_are_valid(rows: Sequence[Row]) -> tuple[bool, str]:
    try:
        build_remap(rows_to_mapping(rows))
    except RemapError as exc:
        return False, str(exc)
    return True, ""


def check_rows(rows: Sequence[Row], *watched: int) -> str | None:
    """Warn when a remapped key is also the PTT or panic key.

    Remapping PTT does not break it, the router watches the physical key, but
    the desktop now sees a different key and that is worth saying out loud
    before somebody wonders why their shortcut moved.
    """
    table = build_remap(rows_to_mapping(rows))
    hit = [code for code in table if code in set(watched)]
    if not hit:
        return None
    names = ", ".join(key_label(c) for c in hit)
    return f"{names} is also your PTT or panic key; the desktop will see the new key instead"


def load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if HAVE_ROUND_TRIP:
        data = _yaml_for(text).load(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    return dict(data or {})


def save_profiles(path: Path, rows: Sequence[Row], name: str) -> None:
    """Write the profile back, touching nothing else in the file.

    Round-trip YAML keeps comments, key order and the shape of everything the
    window has no opinion about. A settings window that rewrites the whole file
    from its own idea of the schema would quietly delete whatever it does not
    know, and this config already has sections the window will not show for a
    long time.
    """
    mapping = rows_to_mapping(rows)

    if HAVE_ROUND_TRIP:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        yaml = _yaml_for(text)
        existing = yaml.load(text) if text else None
        data = existing if existing is not None else {}
        if not isinstance(data, dict):
            data = {}
    else:
        import yaml as pyyaml

        yaml = None
        data = load_raw(path)

    profiles = [
        p for p in (data.get("profiles") or []) if isinstance(p, dict) and p.get("enabled", True)
    ]
    if mapping:
        if profiles:
            # Edit the node in place. Building a replacement list and assigning
            # it throws away the indentation the file already used, so every
            # save would reflow the user's config for no reason.
            keep = profiles[0]
            for stale in [k for k in keep if k not in ("name", "remap")]:
                del keep[stale]
            keep["name"] = name
            keep["remap"] = mapping
        else:
            data["profiles"] = [{"name": name, "remap": mapping}]
    else:
        data.pop("profiles", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    else:
        import yaml as pyyaml

        path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@dataclasses.dataclass(frozen=True, slots=True)
class MicStatus:
    state: str = "unknown"
    source: str = ""
    age: float | None = None
    # Whether the process that wrote the note is still alive. A note is not
    # evidence on its own, so this is what separates "closed" from "the daemon
    # died while it was closed".
    running: bool = False

    @property
    def open(self) -> bool:
        return self.state in (state_mod.OPEN, state_mod.CLOSING)

    @property
    def latched(self) -> bool:
        return self.state == state_mod.LATCHED

    @property
    def text(self) -> str:
        if not self.running:
            return "Демон не запущен"
        if self.state == state_mod.OPEN:
            return "Микрофон открыт"
        if self.state == state_mod.CLOSING:
            return "Микрофон закрывается"
        if self.state == state_mod.LATCHED:
            return "Микрофон залип"
        if self.state == state_mod.CLOSED:
            return "Микрофон закрыт"
        if self.state == state_mod.PANIC:
            return "Паника"
        return "Демон не запущен"

    def describe(self, ptt: str, panic: str) -> str:
        detail = f"PTT: {ptt}"
        if panic:
            detail += f"   Паника: {panic}"
        if self.age is not None:
            detail += f"   Обновлено {self.age:.0f} с назад"
        if not self.running and self.state != "unknown":
            # The note is the only trace of how the process ended, and a panic
            # that stops mid-word is worth surfacing rather than discarding.
            last = MicStatus(
                state=self.state,
                source=self.source,
                age=self.age,
                running=True,
            ).text
            detail += f"   Последнее: {last.lower()}"
        return detail


def read_status() -> MicStatus:
    """The daemon already publishes what it is doing, so read that.

    No socket, no second source of truth, and the window shows the same thing
    `periferia status` prints.

    A note from a process that is no longer running is not a status. It is the
    last thing that was true, and reporting it as current is how a window ends
    up telling you the microphone is open when it has been off since the daemon
    died. The note is kept for the text, but it stops counting as the answer.
    """
    data = state_mod.read()
    if not data:
        return MicStatus()
    return MicStatus(
        state=str(data.get("state", "unknown")),
        source=str(data.get("source") or ""),
        age=state_mod.age(data),
        running=state_mod.is_running(data),
    )


def resolve_label(name: str) -> str:
    code = resolve_key(name)
    return key_label(code) if code is not None else name


def config_path() -> Path:
    from ..core.config import default_config_path

    return default_config_path()
