"""Everything the window needs to know, with no Qt in sight.

The widgets stay thin on purpose. Editing a remap table, refusing an illegal
mapping and saving a config file without destroying the comments somebody wrote
in it are all decisions worth testing, and none of them need a display.
"""

from __future__ import annotations

import dataclasses
import sys
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


# What a profile can be matched on, in the order the window asks for them.
MATCH_FIELDS = ("resource_class", "resource_name", "caption")


@dataclasses.dataclass
class ProfileDraft:
    """One profile as the window holds it: what is being edited, not what is set.

    A draft is deliberately not a ProfileConfig. A config entry carries what the
    file says, including macros this window does not edit, so writing one back
    would either lose those or need fields here for things the window has no
    opinion about.
    """

    name: str = "default"
    match: dict[str, str] = dataclasses.field(default_factory=dict)
    rows: list[Row] = dataclasses.field(default_factory=list)
    speed: float | None = None

    def clean(self) -> ProfileDraft:
        """The same draft with blank fields dropped.

        Saved on every change, so an emptied field has to stop existing rather
        than be written as an empty string, which a profile matcher would treat
        as a criterion that can never match.
        """
        match = {
            field: value.strip()
            for field, value in self.match.items()
            if field in MATCH_FIELDS and str(value).strip()
        }
        return ProfileDraft(
            name=self.name.strip() or "default",
            match=match,
            rows=[(str(k), str(v)) for k, v in self.rows],
            speed=self.speed,
        )


@dataclasses.dataclass
class DeviceDraft:
    """One device entry as the window holds it.

    A draft is deliberately not a DeviceProfile. Audio and processing are kept
    as rows so the window can edit them one key at a time, and the entry is
    written back from those rows without touching the sections or entries the
    window has no opinion about.
    """

    name: str = ""
    match: list[Row] = dataclasses.field(default_factory=list)
    audio: list[Row] = dataclasses.field(default_factory=list)
    processing: list[Row] = dataclasses.field(default_factory=list)

    def clean(self) -> DeviceDraft:
        """A draft with the fields nobody finished filling in dropped.

        A match rule with an empty property or value would be written and then
        match nothing, which looks exactly like a rule that is not there.
        """
        def pairs(rows: list[Row]) -> list[Row]:
            return [
                (str(key).strip(), str(value).strip())
                for key, value in rows
                if str(key).strip() and str(value).strip()
            ]

        return DeviceDraft(
            name=self.name.strip(),
            match=pairs(self.match),
            audio=pairs(self.audio),
            processing=pairs(self.processing),
        )


def draft_from_profile(entry: Any) -> ProfileDraft:
    """A draft holding what the window can edit about one configured profile."""
    return ProfileDraft(
        name=str(getattr(entry, "name", "") or ""),
        match={
            str(k): str(v)
            for k, v in (getattr(entry, "match", None) or {}).items()
            if str(k) in MATCH_FIELDS
        },
        rows=[(str(k), str(v)) for k, v in (getattr(entry, "remap", None) or {}).items()],
        speed=getattr(getattr(entry, "pointer", None), "speed", None),
    )


def drafts_from_config(profiles: Iterable[Any]) -> list[ProfileDraft]:
    return [draft_from_profile(entry) for entry in profiles]


def first_editable(profiles: Iterable[Any]) -> ProfileDraft | None:
    """The profile the window opens on.

    The first one that changes anything. A fallback profile with an empty table
    is a real thing people write, but opening the editor on a profile that does
    nothing gives a blank page and the impression that nothing is configured.
    """
    drafts = drafts_from_config(profiles)
    for draft in drafts:
        if draft.rows or draft.match or draft.speed is not None:
            return draft
    return drafts[0] if drafts else None


def draft_from_device(entry: Any) -> DeviceDraft:
    """A draft holding what the window can edit about one device entry."""
    return DeviceDraft(
        name=str(getattr(entry, "name", "") or ""),
        match=[(str(k), str(v)) for k, v in (getattr(entry, "match", None) or {}).items()],
        audio=[(str(k), str(v)) for k, v in (getattr(entry, "audio", None) or {}).items()],
        processing=[
            (str(k), str(v)) for k, v in (getattr(entry, "processing", None) or {}).items()
        ],
    )


def drafts_from_devices(devices: Iterable[Any]) -> list[DeviceDraft]:
    return [draft_from_device(entry) for entry in devices]


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


def rows_to_dict(rows: Sequence[Row]) -> dict[str, str]:
    """Rows as a mapping, refusing a key written twice.

    The same shape as rows_to_mapping but for the plain key/value tables a
    device entry is made of (match rules and audio/processing overrides),
    where a repeated key is an ambiguity that is not worth risking.
    """
    out: dict[str, str] = {}
    for key, value in rows:
        if key in out:
            raise ValueError(f"{key} is written twice in the same entry")
        out[key] = value
    return out


def _coerce_value(hint: Any, value: str) -> Any:
    if hint is bool:
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
        return value
    if hint is int:
        try:
            return int(value)
        except ValueError:
            return value
    if hint is float:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def typed_section(cls: type, rows: Sequence[Row]) -> dict[str, Any]:
    """Table rows as the typed values the config stores them as.

    The window keeps every section as rows of text, so `target_volume` reads
    as the string "1.0". Written into the file as a string it would not be the
    number the field is, and the strict reader would refuse it on the next
    startup. The field's own type decides what "1.0" means here; a value that
    cannot be that type stays text and the validator refuses it with the key
    named.
    """
    import typing

    hints = typing.get_type_hints(cls)
    out: dict[str, Any] = {}
    for key, value in rows_to_dict(rows).items():
        out[key] = _coerce_value(hints.get(key, str), value)
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


def save_draft(
    path: Path,
    draft: ProfileDraft,
    previous_name: str | None = None,
) -> None:
    """Write one profile back, touching nothing else in the file.

    Round-trip YAML keeps comments, key order and the shape of everything the
    window has no opinion about. A settings window that rewrites the whole file
    from its own idea of the schema would quietly delete whatever it does not
    know, and this config already has sections the window will not show for a
    long time.

    The profile is found by `previous_name` when it is being renamed, and by its
    own name otherwise. Only `name`, `match`, `remap` and `pointer` are written:
    a profile's macros and its enabled flag stay whatever the file said.
    """
    cleaned = draft.clean()
    mapping = rows_to_mapping(cleaned.rows)

    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if HAVE_ROUND_TRIP:
        yaml = _yaml_for(text)
        data = yaml.load(text) if text else None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            data = {}
    else:
        import yaml as pyyaml

        yaml = None
        data = load_raw(path)

    profiles = data.get("profiles")
    if profiles is None:
        profiles = []
    wanted = previous_name or cleaned.name
    existing = next(
        (
            p
            for p in profiles
            if isinstance(p, dict) and str(p.get("name", "")) == wanted
        ),
        None,
    )
    if existing is None:
        existing = {}
        profiles.append(existing)

    existing["name"] = cleaned.name
    if cleaned.match:
        existing["match"] = cleaned.match
    else:
        existing.pop("match", None)
    if mapping:
        existing["remap"] = mapping
    else:
        existing.pop("remap", None)
    if cleaned.speed is not None:
        existing["pointer"] = {"speed": cleaned.speed}
    else:
        existing.pop("pointer", None)

    data["profiles"] = profiles

    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    else:
        import yaml as pyyaml

        path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def delete_draft(path: Path, name: str) -> bool:
    """Remove one profile. False when it was not there to begin with.

    Macros defined inside the profile go with it. They are the profile's own,
    and leaving them behind under a name nothing claims would make them
    unreachable in a way that is hard to notice.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if HAVE_ROUND_TRIP:
        yaml = _yaml_for(text)
        data = yaml.load(text) if text else None
        if not isinstance(data, dict):
            return False
    else:
        import yaml as pyyaml

        yaml = None
        data = load_raw(path)

    profiles = data.get("profiles") or []
    kept = [p for p in profiles if not (isinstance(p, dict) and str(p.get("name", "")) == name)]
    if len(kept) == len(profiles):
        return False
    if kept:
        data["profiles"] = kept
    else:
        data.pop("profiles", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    else:
        import yaml as pyyaml

        path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True


def check_draft(draft: ProfileDraft, others: Sequence[ProfileDraft] = ()) -> str | None:
    """What is wrong with this draft, or None if it would load.

    Runs the same checks the file does, so the window refuses before it saves
    rather than the daemon refusing after it has grabbed the keyboard.
    """
    from ..core.config import PointerConfig, ProfileConfig

    cleaned = draft.clean()
    profiles = [
        ProfileConfig(
            name=d.name,
            match=dict(d.match),
            remap=rows_to_mapping(d.rows),
            pointer=PointerConfig(speed=d.speed) if d.speed is not None else None,
        )
        for d in [cleaned, *others]
    ]
    from ..core import validate

    report = validate.check_profiles(profiles)
    if report.errors:
        return str(report.errors[0])

    # The router's own rules as well as the config's. A modifier target is only
    # refused where the table is built, and a window that saves without asking
    # leaves the daemon to refuse after it has grabbed the keyboard.
    try:
        build_remap(rows_to_mapping(cleaned.rows))
    except RemapError as exc:
        return str(exc)
    return None


def save_device(
    path: Path,
    draft: DeviceDraft,
    previous_name: str | None = None,
) -> None:
    """Write one device entry back, touching nothing else in the file.

    Round-trip YAML for the same reason save_draft uses it: the config tries to
    stay the file the user wrote, with its comments and its other sections.

    The entry is found by `previous_name` when it is being renamed, and by its
    own name otherwise. Only `name`, `match`, `audio` and `processing` are
    written; the entry's `enabled` flag stays whatever the file said.
    """
    from ..core.config import AudioConfig, ProcessingConfig

    cleaned = draft.clean()
    match = rows_to_dict(cleaned.match)
    audio = typed_section(AudioConfig, cleaned.audio)
    processing = typed_section(ProcessingConfig, cleaned.processing)

    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if HAVE_ROUND_TRIP:
        yaml = _yaml_for(text)
        data = yaml.load(text) if text else None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            data = {}
    else:
        yaml = None
        data = load_raw(path)

    devices = data.get("devices")
    if devices is None:
        devices = []
    wanted = previous_name or cleaned.name
    existing = next(
        (
            d
            for d in devices
            if isinstance(d, dict) and str(d.get("name", "")) == wanted
        ),
        None,
    )
    if existing is None:
        existing = {}
        devices.append(existing)

    existing["name"] = cleaned.name
    if match:
        existing["match"] = match
    else:
        existing.pop("match", None)
    if audio:
        existing["audio"] = audio
    else:
        existing.pop("audio", None)
    if processing:
        existing["processing"] = processing
    else:
        existing.pop("processing", None)

    data["devices"] = devices
    _dump(path, data, yaml)


def delete_device(path: Path, name: str) -> bool:
    """Remove one device entry. False when it was not there to begin with."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if HAVE_ROUND_TRIP:
        yaml = _yaml_for(text)
        data = yaml.load(text) if text else None
        if not isinstance(data, dict):
            return False
    else:
        yaml = None
        data = load_raw(path)

    devices = data.get("devices") or []
    kept = [
        d
        for d in devices
        if not (isinstance(d, dict) and str(d.get("name", "")) == name)
    ]
    if len(kept) == len(devices):
        return False
    if kept:
        data["devices"] = kept
    else:
        data.pop("devices", None)

    _dump(path, data, yaml)
    return True


def check_device_draft(draft: DeviceDraft, others: Sequence[DeviceDraft] = ()) -> str | None:
    """What is wrong with this device draft, or None if it would load.

    Runs the same value checks the file does and the same positional checks
    check_devices makes, so the window refuses a bad value and a doomed entry
    before it writes either.
    """
    from ..core import validate
    from ..core.config import AudioConfig, ProcessingConfig, validate_device

    cleaned = draft.clean()
    if not cleaned.name:
        return "задайте имя устройства, прежде чем сохранять"
    try:
        entries = [
            validate_device(
                entry.name,
                rows_to_dict(entry.match),
                typed_section(AudioConfig, entry.audio),
                typed_section(ProcessingConfig, entry.processing),
            )
            for entry in [cleaned, *others]
        ]
    except ValueError as exc:
        return str(exc)
    report = validate.check_devices(entries)
    if report.errors:
        return str(report.errors[0])
    return None


# One colour per state, shared by the tray, the overlay and the window, so the
# three can never drift apart and leave the tray green while the window says the
# microphone is closed.
STATE_COLOURS = {
    state_mod.OPEN: "#2e7d32",
    state_mod.CLOSING: "#9a6700",
    state_mod.LATCHED: "#b26a00",
    state_mod.PANIC: "#b3261e",
    state_mod.CLOSED: "#5f6368",
    "unknown": "#5f6368",
}


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
    def colour(self) -> str:
        if not self.running:
            return STATE_COLOURS["unknown"]
        return STATE_COLOURS.get(self.state, STATE_COLOURS["unknown"])

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


@dataclasses.dataclass
class TrayLook:
    """How the tray icon should look for one status.

    Kept apart from the icon itself so it can be decided without a display.
    What colour a state gets is a rule the user reads, not a drawing detail.
    """

    colour: str
    text: str
    # Latched is the state nobody can hear: the key was released and the mic is
    # still open. It has to be told apart from open, not shown as another shade
    # of green.
    attention: bool = False


def tray_look(status: MicStatus) -> TrayLook:
    """The colour and wording for a status.

    The daemon is not running is deliberately the same grey as closed, with the
    wording doing the work. A red tray for a daemon that is simply not started
    would cry wolf every time the machine boots.
    """
    if not status.running:
        return TrayLook(STATE_COLOURS["unknown"], "Periferia: демон не запущен")
    if status.state == state_mod.OPEN:
        return TrayLook(STATE_COLOURS[state_mod.OPEN], "Periferia: микрофон открыт")
    if status.state == state_mod.CLOSING:
        return TrayLook(STATE_COLOURS[state_mod.CLOSING], "Periferia: микрофон закрывается")
    if status.state == state_mod.LATCHED:
        return TrayLook(STATE_COLOURS[state_mod.LATCHED], "Periferia: микрофон залип", True)
    if status.state == state_mod.PANIC:
        return TrayLook(STATE_COLOURS[state_mod.PANIC], "Periferia: паника", True)
    return TrayLook(STATE_COLOURS[state_mod.CLOSED], "Periferia: микрофон закрыт")


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


def save_macro(
    path: Path,
    name: str,
    steps: Sequence[tuple[str, int, int]],
    bind: str = "",
    profiles: Sequence[str] = (),
) -> None:
    """Write one macro back into the config, touching nothing else.

    `steps` is (key, at_ms, hold_ms). The offset is counted from the start of the
    macro rather than from the previous release, so a chord survives the round
    trip: two keys whose offsets overlap stay overlapping in the file instead of
    being flattened into consecutive taps by the writer."""
    """Write one macro back into the config, touching nothing else.

    Round-trip YAML for the same reason save_profiles uses it: rewriting the
    file from the schema would delete every section this function does not know
    about, and the config has several. Comments in particular would survive
    neither approach.

    A macro with the same name is replaced rather than appended to, because two
    macros sharing a name is ambiguous about which one a trigger plays.

    `profiles` names the profiles that should carry their own copy. Each gets the
    same steps under the same name, which is how a macro can be bound to one key
    everywhere and another inside a game.
    """
    if HAVE_ROUND_TRIP:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        yaml = _yaml_for(text)
        existing = yaml.load(text) if text else None
        data = existing if existing is not None else {}
        if not isinstance(data, dict):
            data = {}
    else:
        yaml = None
        data = load_raw(path)

    def as_list() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for profile in profiles:
            for entry in (data.get("profiles") or []):
                if isinstance(entry, dict) and entry.get("name") == profile:
                    out.append(entry)
        return out

    entry: dict[str, Any] = {
        "name": name,
        "bind": bind,
        "steps": [
            {"key": key, "at_ms": int(at), "hold_ms": int(hold)} for key, at, hold in steps
        ],
    }

    def put(container: dict[str, Any]) -> None:
        existing_list = [m for m in (container.get("macros") or []) if isinstance(m, dict)]
        kept = [m for m in existing_list if m.get("name") != name]
        # Appended at the end so the file reads in the order things were made.
        kept.append(entry)
        container["macros"] = kept

    if not profiles:
        put(data)
    else:
        touched = as_list()
        if not touched:
            # Fall back to writing it globally rather than dropping the
            # recording. The warning says so plainly, because a macro bound to a
            # key and placed globally is live everywhere, which is not what
            # --in-profile asked for and has to be visible in the terminal.
            print(
                f"periferia: no profile named {', '.join(profiles)},"
                " so the macro was saved globally instead and will apply"
                " in every window. Check the profile name.",
                file=sys.stderr,
            )
        for profile in touched:
            put(profile)
        if touched:
            return _dump(path, data, yaml)
    put(data)
    return _dump(path, data, yaml)


def _dump(path: Path, data: dict[str, Any], yaml: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    else:
        import yaml as pyyaml

        path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    else:
        import yaml as pyyaml

        path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")
