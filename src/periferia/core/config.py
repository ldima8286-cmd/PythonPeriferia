from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "PERIFERIA_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"

_APP_DIRS = {
    "xdg": Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"),
    "legacy": Path.home() / ".periferia",
}


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


@dataclasses.dataclass(slots=True)
class AudioConfig:
    """Which microphone to use and how the gate behaves."""

    physical_source: str = "auto"
    virtual_name: str = "PeriferiaMic"

    attack_ms: int = 10
    release_ms: int = 60
    hold_ms: int = 200
    curve: str = "exp"
    target_volume: float = 1.0
    start_muted: bool = True

    enabled: bool = True


@dataclasses.dataclass(slots=True)
class PttConfig:
    """The key that opens the microphone, in physical-key terms."""

    ptt_key: str = "auto"
    panic_key: str = "KEY_F12"
    device: str = "auto"

    ignore_repeat: bool = True

    # Hold the key at least this long to latch the microphone open, in ms.
    # While latched, releasing the key does not close it: the next press does.
    # For the times you need both hands free. 0 disables latching.
    latch_ms: int = 0

    # Cut off a key that has been held this long, in ms. 0 disables it.
    # A last resort against a press that never gets a release.
    max_press_ms: int = 300000

    enabled: bool = True


@dataclasses.dataclass(slots=True)
class ProcessingConfig:
    """PipeWire module-echo-cancel. No DSP in this project."""

    noise_suppression: bool = True
    echo_cancellation: bool = True
    voice_detect: bool = True
    tail_length_ms: int = 200
    extra_props: dict[str, str] = dataclasses.field(default_factory=dict)

    # Publish the virtual microphone as one mono channel carried by the left
    # input channel, instead of letting every application downmix the two
    # captured channels itself. The two channels of the measured hardware are
    # not copies of one mono input (they really differ), and averaging them
    # cancels the band above 3.5 kHz; keeping one channel keeps the whole band.
    stereo_to_mono: bool = True

    # Which input channel carries the mono one. PipeWire names a channel by its
    # position, "front-left" being the left one, and the step from the measured
    # hardware was chosen because its left channel turned out louder and more
    # intact. A card oriented the other way around wants "front-right".
    mono_from: str = "front-left"

    enabled: bool = True


@dataclasses.dataclass(slots=True)
class DeviceProfile:
    """Settings that follow a capture device, matched by the properties it has.

    Nominal gain, whether echo cancellation is worth anything and which channel
    to keep are properties of the hardware, not of the machine: a quiet analog
    jack and a USB headset want different answers. Each entry names the device
    and overrides the matching top-level section key by key.

    `match` is compared to the properties PipeWire reports for a source —
    `node.name`, `device.bus` ("pci", "usb"), `device.vendor.name`,
    `device.product.name`, `alsa.card_name`, `device.form_factor`, and anything
    else the card carries. Every rule has to agree for the entry to apply, and
    the first entry that applies wins. An entry with no `match` applies to any
    device, which makes it the fallback; it earns that name best when it sits
    last. `periferia sources` shows which entry a device would get.

    The audio/processing values here are overrides: an absent key keeps the
    value from the top-level `audio:`/`processing:` section. Both sections take
    only keys the real section would, and anything else is rejected like any
    unknown key, so nobody has to try the audio section to learn that `volume`
    is not one of its keys.
    """

    name: str = ""
    enabled: bool = True
    match: dict[str, str] = dataclasses.field(default_factory=dict)
    audio: dict[str, Any] = dataclasses.field(default_factory=dict)
    processing: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class ProfileConfig:
    """One keyboard layout to switch to: which physical key becomes which.

    `match` says which windows this profile belongs to, by resource class or
    resource name as the compositor reports them. A profile with no `match` is
    the fallback for everything nothing else claimed.

    Each criterion takes one value or a list of them.

    `match` deliberately has no way to name a window by caption. Captions change
    with the window's contents, so a profile matched on one would apply and stop
    applying as the same program showed different text.
    """

    name: str = "default"
    enabled: bool = True
    match: dict[str, str | list[str]] = dataclasses.field(default_factory=dict)
    remap: dict[str, str] = dataclasses.field(default_factory=dict)
    pointer: PointerConfig | None = None
    macros: list[MacroConfig] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(slots=True)
class PointerConfig:
    """How fast the mouse pointer moves while this profile applies.

    `speed` is the same number the desktop settings show as pointer speed, where
    1.0 is what the hardware was built with. It is not DPI: the DPI a mouse
    reports is a property of the mouse, and only software can pretend otherwise
    by scaling the pointer.

    The pointer is not the keyboard, so this does not travel through uinput. It
    is written into the compositor's own configuration file, which means it is
    the compositor that applies it and only a compositor that can be asked will.
    """

    speed: float | None = None


@dataclasses.dataclass(slots=True)
class MacroStep:
    """One keypress inside a macro, with the timing that makes it a recording.

    `at_ms` is when the key goes down, counted from the start of the macro. Two
    keys can share one offset range, which is what lets a chord be recorded:
    Ctrl down at 0, C down at 40, C up at 60, Ctrl up at 120.

    `gap_ms` is the older timing, a pause from the previous key coming back up.
    It is still read so that macros recorded before chords existed keep playing
    as they did, and is written only when `at_ms` is absent.

    `hold_ms` exists because a key that is pressed and never released makes
    everything typed after it come out as one held modifier.
    """

    key: str = ""
    at_ms: int | None = None
    gap_ms: int = 0
    hold_ms: int = 40


@dataclasses.dataclass(slots=True)
class MacroConfig:
    """A recorded sequence of keypresses, and the key that plays it back.

    `bind` is empty when the macro is not on a key. A macro with no bind is kept
    but inert, which is what lets `macro record` save first and ask about the key
    afterwards without losing the recording.
    """

    name: str = ""
    bind: str = ""
    steps: list[MacroStep] = dataclasses.field(default_factory=list)
    enabled: bool = True


@dataclasses.dataclass(slots=True)
class LogConfig:
    level: str = "info"
    file: str | None = None

    enabled: bool = True


def _unbox(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _unbox(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: _unbox(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unbox(v) for v in value]
    return value


@dataclasses.dataclass(slots=True)
class Config:
    audio: AudioConfig = dataclasses.field(default_factory=AudioConfig)
    ptt: PttConfig = dataclasses.field(default_factory=PttConfig)
    processing: ProcessingConfig = dataclasses.field(default_factory=ProcessingConfig)
    log: LogConfig = dataclasses.field(default_factory=LogConfig)
    macros: list[MacroConfig] = dataclasses.field(default_factory=list)
    profiles: list[ProfileConfig] = dataclasses.field(default_factory=list)
    devices: list[DeviceProfile] = dataclasses.field(default_factory=list)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio": _unbox(self.audio),
            "ptt": _unbox(self.ptt),
            "processing": _unbox(self.processing),
            "log": _unbox(self.log),
            "macros": [_unbox(m) for m in self.macros],
            "profiles": [_unbox(p) for p in self.profiles],
            "devices": [_unbox(d) for d in self.devices],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class _Rule:
    """What one config value is allowed to be.

    A schema is only worth having if a bad value fails where it is read, with
    the key named, instead of inside a timing loop at 2 a.m. These rules say
    the shape a value may take; the dataclasses say which keys exist, and the
    two together refuse every mistake a config can carry.
    """

    kind: str
    minimum: float | None = None
    enum: tuple[str, ...] = ()


_RULES: dict[type, dict[str, _Rule]] = {
    AudioConfig: {
        "physical_source": _Rule("string"),
        "virtual_name": _Rule("string"),
        "attack_ms": _Rule("int", minimum=0),
        "release_ms": _Rule("int", minimum=0),
        "hold_ms": _Rule("int", minimum=0),
        "curve": _Rule("enum", enum=("exp", "linear", "s_curve")),
        "target_volume": _Rule("number", minimum=0),
        "start_muted": _Rule("bool"),
        "enabled": _Rule("bool"),
    },
    PttConfig: {
        "ptt_key": _Rule("string"),
        "panic_key": _Rule("string"),
        "device": _Rule("string"),
        "ignore_repeat": _Rule("bool"),
        "latch_ms": _Rule("int", minimum=0),
        "max_press_ms": _Rule("int", minimum=0),
        "enabled": _Rule("bool"),
    },
    ProcessingConfig: {
        "noise_suppression": _Rule("bool"),
        "echo_cancellation": _Rule("bool"),
        "voice_detect": _Rule("bool"),
        "tail_length_ms": _Rule("int", minimum=0),
        "extra_props": _Rule("map_strings"),
        "stereo_to_mono": _Rule("bool"),
        "mono_from": _Rule("string"),
        "enabled": _Rule("bool"),
    },
    LogConfig: {
        "level": _Rule("enum", enum=("debug", "info", "warning", "error")),
        "file": _Rule("string_or_none"),
        "enabled": _Rule("bool"),
    },
    DeviceProfile: {
        "name": _Rule("string"),
        "enabled": _Rule("bool"),
        "match": _Rule("map_strings"),
    },
    ProfileConfig: {
        "name": _Rule("string"),
        "enabled": _Rule("bool"),
        "match": _Rule("map_string_or_list"),
        "remap": _Rule("map_strings"),
    },
    PointerConfig: {
        "speed": _Rule("number_or_none"),
    },
    MacroConfig: {
        "name": _Rule("string"),
        "bind": _Rule("string"),
        "enabled": _Rule("bool"),
    },
    MacroStep: {
        "key": _Rule("string"),
        "at_ms": _Rule("int_or_none", minimum=0),
        "gap_ms": _Rule("int", minimum=0),
        "hold_ms": _Rule("int", minimum=0),
    },
}


def _check_value(value: Any, rule: _Rule, where: str) -> None:
    if rule.kind.endswith("_or_none") and value is None:
        return
    if rule.kind in ("int", "int_or_none"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}: expected a whole number, got {value!r}")
    elif rule.kind in ("number", "number_or_none"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: expected a number, got {value!r}")
    elif rule.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{where}: expected true or false, got {value!r}")
    elif rule.kind == "enum":
        if not isinstance(value, str) or value not in rule.enum:
            expected = ", ".join(rule.enum)
            raise ValueError(f"{where}: expected one of {expected}, got {value!r}")
    elif rule.kind in ("string", "string_or_none"):
        if not isinstance(value, str):
            raise ValueError(f"{where}: expected text, got {value!r}")
    elif rule.kind in ("map_strings", "map_string_or_list"):
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expected a mapping, got {value!r}")
        for key, item in value.items():
            if rule.kind == "map_strings":
                if not isinstance(item, str):
                    raise ValueError(f"{where}.{key}: expected text, got {item!r}")
            elif isinstance(item, list):
                if not all(isinstance(x, str) for x in item):
                    raise ValueError(f"{where}.{key}: expected text or a list"
                                     f" of text, got {item!r}")
            elif not isinstance(item, str):
                raise ValueError(f"{where}.{key}: expected text or a list"
                                 f" of text, got {item!r}")
    if (
        rule.minimum is not None
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value < rule.minimum
    ):
        raise ValueError(f"{where}: must not be negative")


def _check(cls: type, data: dict[str, Any], where: str) -> None:
    """Refuse values that are the right key and the wrong kind of thing.

    The shape a value may take is declared in `_RULES`; why the shape matters
    is answered by the failing message naming the key. A section is checked
    only for the keys that are actually written, so the defaults never need
    defending.
    """
    for key, value in data.items():
        rule = _RULES.get(cls, {}).get(key)
        if rule is not None:
            _check_value(value, rule, f"{where}.{key}")


def _build(cls: type, data: Any, where: str = "") -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        message = f"expected a mapping for {cls.__name__}, got {type(data).__name__}"
        raise ValueError(f"{where}: {message}" if where else message)

    fields = {f.name for f in dataclasses.fields(cls)}
    known = {k: v for k, v in data.items() if k in fields}
    unknown = set(data) - fields
    if unknown:
        message = f"unknown keys in {cls.__name__}: {sorted(unknown)}"
        raise ValueError(f"{where}: {message}" if where else message)
    _check(cls, known, where or cls.__name__)
    return cls(**known)


def _build_macros(data: Any, where: str) -> list[MacroConfig]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"expected a list for {where}, got {type(data).__name__}")
    out: list[MacroConfig] = []
    for index, item in enumerate(data):
        try:
            macro = _build(MacroConfig, item)
        except ValueError as exc:
            raise ValueError(f"{where}[{index}]: {exc}") from None
        if not isinstance(item, dict):
            raise ValueError(f"{where}[{index}]: expected a mapping")
        steps = item.get("steps") or []
        if not isinstance(steps, list):
            raise ValueError(f"{where}[{index}].steps: expected a list")
        macro.steps = [
            _build(MacroStep, step, where=f"{where}[{index}].steps[{step_index}]")
            for step_index, step in enumerate(steps)
        ]
        out.append(macro)
    return out


def _build_profiles(data: Any) -> list[ProfileConfig]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"expected a list for profiles, got {type(data).__name__}")
    out = []
    for index, item in enumerate(data):
        try:
            out.append(_build(ProfileConfig, item))
        except ValueError as exc:
            raise ValueError(f"profiles[{index}]: {exc}") from None
        if isinstance(item, dict):
            out[-1].match = out[-1].match or {}
            out[-1].remap = out[-1].remap or {}
            out[-1].macros = _build_macros(item.get("macros"), f"profiles[{index}].macros")
            pointer = item.get("pointer")
            if pointer is not None:
                try:
                    out[-1].pointer = _build(PointerConfig, pointer)
                except ValueError as exc:
                    raise ValueError(f"profiles[{index}].pointer: {exc}") from None
    return out


def _override(cls: type, data: Any, where: str) -> dict[str, Any]:
    """Validate an override section without building a whole config from it.

    Only the keys a section really takes are accepted, spelled against the same
    field list the section itself is built with. Values are left as written:
    the top-level section supplies the base, so an absent key must not sneak in
    a default here.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(data).__name__}")
    unknown = set(data) - {f.name for f in dataclasses.fields(cls)}
    if unknown:
        raise ValueError(f"{where}: unknown keys: {sorted(unknown)}")
    _check(cls, data, where)
    return dict(data)


def _build_devices(data: Any) -> list[DeviceProfile]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"expected a list for devices, got {type(data).__name__}")
    out = []
    for index, item in enumerate(data):
        where = f"devices[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected a mapping")
        try:
            device = _build(DeviceProfile, item)
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from None
        device.match = device.match or {}
        device.audio = _override(AudioConfig, item.get("audio") or {}, f"{where}.audio")
        device.processing = _override(
            ProcessingConfig, item.get("processing") or {}, f"{where}.processing"
        )
        out.append(device)
    return out


def _props_match(rules: dict[str, str], props: dict[str, Any]) -> bool:
    """Whether one match rule agrees with the properties a source has.

    A rule is a flat "property has this exact value", which keeps matching
    predictable; everything a rule can say is visible in `periferia props`.
    """
    return all(str(props.get(key)) == str(wanted) for key, wanted in rules.items())


def match_profile(cfg: Config, props: dict[str, Any] | None) -> DeviceProfile | None:
    """The first enabled device entry that owns `props`, if any.

    An entry with no rules matches anything, which is what makes it the
    fallback profile. Disabled entries are skipped as if they were not there.
    """
    props = props or {}
    for device in cfg.devices:
        if not device.enabled:
            continue
        if _props_match(device.match, props):
            return device
    return None


def validate_device(
    name: str,
    match: dict[str, Any],
    audio: dict[str, Any],
    processing: dict[str, Any],
) -> DeviceProfile:
    """Build one device entry the way the file would, refusing what would not load.

    The GUI calls this before writing an entry, so a draft that says
    `target_volume: loud` is refused here with the key named instead of after
    the daemon has already applied it. Returns the entry so the caller can
    check it further, e.g. with `validate.check_devices`.
    """
    profile: DeviceProfile = _build(
        DeviceProfile,
        {"name": name, "match": match},
        "device",
    )
    profile.match = profile.match or {}
    profile.audio = _override(AudioConfig, dict(audio), "device.audio")
    profile.processing = _override(ProcessingConfig, dict(processing), "device.processing")
    return profile


def effective(cfg: Config, profile: DeviceProfile | None) -> tuple[AudioConfig, ProcessingConfig]:
    """The settings in force for one device: defaults overlaid by its entry.

    The top-level sections stay the base so a config that never mentions
    `devices:` keeps working as before, and a device entry only has to write
    what differs from them.
    """
    audio = cfg.audio
    processing = cfg.processing
    if profile is not None:
        if profile.audio:
            audio = dataclasses.replace(audio, **profile.audio)
        if profile.processing:
            processing = dataclasses.replace(processing, **profile.processing)
    return audio, processing


def find_config(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        return _expand(str(explicit))
    env = os.environ.get(CONFIG_ENV)
    if env:
        return _expand(env)
    for base in (_APP_DIRS["xdg"] / "periferia", _APP_DIRS["legacy"]):
        candidate = base / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load(explicit: str | Path | None = None) -> Config:
    path = find_config(explicit)
    if path is None:
        return Config()

    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    return Config(
        audio=_build(AudioConfig, data.get("audio"), "audio"),
        ptt=_build(PttConfig, data.get("ptt"), "ptt"),
        processing=_build(ProcessingConfig, data.get("processing"), "processing"),
        macros=_build_macros(data.get("macros"), "macros"),
        log=_build(LogConfig, data.get("log"), "log"),
        profiles=_build_profiles(data.get("profiles")),
        devices=_build_devices(data.get("devices")),
        path=path,
    )


def user_config_dir() -> Path:
    path = _APP_DIRS["xdg"] / "periferia"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return user_config_dir() / DEFAULT_CONFIG_NAME
