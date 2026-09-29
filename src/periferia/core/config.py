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
    channel_map: str = "mono"

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
    release_on_focus_loss: bool = True

    enabled: bool = True


@dataclasses.dataclass(slots=True)
class ProcessingConfig:
    """PipeWire module-echo-cancel. No DSP in this project."""

    noise_suppression: bool = True
    echo_cancellation: bool = True
    voice_detect: bool = False
    tail_length_ms: int = 200
    extra_props: dict[str, str] = dataclasses.field(default_factory=dict)

    enabled: bool = True


@dataclasses.dataclass(slots=True)
class TrayConfig:
    show: bool = True
    overlay: bool = False
    overlay_corner: str = "top-right"

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
    tray: TrayConfig = dataclasses.field(default_factory=TrayConfig)
    log: LogConfig = dataclasses.field(default_factory=LogConfig)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio": _unbox(self.audio),
            "ptt": _unbox(self.ptt),
            "processing": _unbox(self.processing),
            "tray": _unbox(self.tray),
            "log": _unbox(self.log),
        }


def _build(cls: type, data: Any) -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")

    fields = {f.name for f in dataclasses.fields(cls)}
    known = {k: v for k, v in data.items() if k in fields and k != "enabled"}
    unknown = set(data) - fields
    if unknown:
        raise ValueError(f"unknown keys in {cls.__name__}: {sorted(unknown)}")
    return cls(**known)


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
        tray=_build(TrayConfig, data.get("tray")),
        log=_build(LogConfig, data.get("log")),
        path=path,
    )


def user_config_dir() -> Path:
    path = _APP_DIRS["xdg"] / "periferia"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return user_config_dir() / DEFAULT_CONFIG_NAME
