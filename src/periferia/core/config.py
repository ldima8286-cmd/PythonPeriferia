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

    enabled: bool = True


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
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio": _unbox(self.audio),
            "ptt": _unbox(self.ptt),
            "processing": _unbox(self.processing),
            "log": _unbox(self.log),
            "macros": [_unbox(m) for m in self.macros],
            "profiles": [_unbox(p) for p in self.profiles],
        }


def _build(cls: type, data: Any) -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")

    fields = {f.name for f in dataclasses.fields(cls)}
    known = {k: v for k, v in data.items() if k in fields}
    unknown = set(data) - fields
    if unknown:
        raise ValueError(f"unknown keys in {cls.__name__}: {sorted(unknown)}")
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
            _build(MacroStep, step) for step in steps
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
            out[-1].macros = _build_macros(item.get("macros"), f"profiles[{index}].macros")
            pointer = item.get("pointer")
            if pointer is not None:
                try:
                    out[-1].pointer = _build(PointerConfig, pointer)
                except ValueError as exc:
                    raise ValueError(f"profiles[{index}].pointer: {exc}") from None
    return out


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
        audio=_build(AudioConfig, data.get("audio")),
        ptt=_build(PttConfig, data.get("ptt")),
        processing=_build(ProcessingConfig, data.get("processing")),
        macros=_build_macros(data.get("macros"), "macros"),
        log=_build(LogConfig, data.get("log")),
        profiles=_build_profiles(data.get("profiles")),
        path=path,
    )


def user_config_dir() -> Path:
    path = _APP_DIRS["xdg"] / "periferia"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return user_config_dir() / DEFAULT_CONFIG_NAME
