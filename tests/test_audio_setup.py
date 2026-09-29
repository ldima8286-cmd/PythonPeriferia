from __future__ import annotations

import importlib

import pytest

# See the note in test_audio_ramp.py: importlib resolves these submodules
# reliably where the import statement does not.
config_mod = importlib.import_module("periferia.core.config")
audio_mod = importlib.import_module("periferia.modules.audio")

AudioConfig = config_mod.AudioConfig
VirtualMic = audio_mod.VirtualMic


class _FakePipewire:
    """Records module loads/unloads so a leak shows up as a test failure."""

    def __init__(self, source: str | None) -> None:
        self.source = source
        self.loaded: list[tuple[str, list[str]]] = []
        self.unloaded: list[int] = []
        self._next_id = 77

    def find_source(self, name: str) -> str | None:
        return None

    def source_exists(self, name: str) -> bool:
        return False

    def source_by_module(self, module_id: int) -> str | None:
        return self.source

    def load_module(self, kind: str, args: list[str]) -> int:
        self.loaded.append((kind, args))
        return self._next_id

    def unload_module(self, module_id: int) -> bool:
        self.unloaded.append(module_id)
        return True


def _mic(monkeypatch: pytest.MonkeyPatch, source: str | None) -> tuple[VirtualMic, _FakePipewire]:
    fake = _FakePipewire(source)
    monkeypatch.setattr(audio_mod, "pipewire", fake)
    monkeypatch.setattr(audio_mod, "pick_physical_source", lambda preferred: "hw:physical")
    mic = VirtualMic(AudioConfig(attack_ms=2000), slider=lambda s, v: None)
    return mic, fake


def test_setup_unloads_loopback_when_source_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dangling module-loopback keeps capturing the microphone even though the
    # daemon reported a failed start, so it has to be unloaded on this path.
    mic, fake = _mic(monkeypatch, source=None)

    assert mic.setup() is None
    assert fake.loaded, "the loopback was expected to load"
    assert fake.unloaded == [77], "the loaded loopback module was leaked"
    assert mic._module_id is None, "a stale module id would block a later teardown"


def test_setup_keeps_the_module_when_source_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # Control case: on the happy path nothing may be unloaded.
    mic, fake = _mic(monkeypatch, source="alsa_input.periferia")

    assert mic.setup() == "alsa_input.periferia"
    assert fake.unloaded == []
    assert mic._module_id == 77
