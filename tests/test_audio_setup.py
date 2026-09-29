from __future__ import annotations

import importlib

import pytest

# See the note in test_audio_ramp.py: importlib resolves these submodules
# reliably where the import statement does not.
config_mod = importlib.import_module("periferia.core.config")
audio_mod = importlib.import_module("periferia.modules.audio")
processing_mod = importlib.import_module("periferia.modules.processing")

AudioConfig = config_mod.AudioConfig
ProcessingConfig = config_mod.ProcessingConfig
VirtualMic = audio_mod.VirtualMic
MicProcessing = processing_mod.MicProcessing


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


def _processing(
    monkeypatch: pytest.MonkeyPatch, source: str | None, cfg: ProcessingConfig | None = None
) -> tuple[MicProcessing, _FakePipewire]:
    fake = _FakePipewire(source)
    monkeypatch.setattr(processing_mod, "pipewire", fake)
    return MicProcessing(cfg or ProcessingConfig()), fake


def _mic(monkeypatch: pytest.MonkeyPatch) -> VirtualMic:
    monkeypatch.setattr(audio_mod, "pipewire", _FakePipewire(None))
    return VirtualMic(AudioConfig(attack_ms=2000), slider=lambda s, v: None)


def test_named_module_unloads_when_it_creates_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A module that registered but produced no source would otherwise keep
    # capturing the microphone after the daemon reported a failed start.
    processing, fake = _processing(monkeypatch, source=None)

    assert processing.start("hw:physical", name="PeriferiaMic") is None
    assert fake.loaded, "the module was expected to load"
    assert fake.unloaded == [77], "the loaded module was leaked"


def test_named_module_stays_loaded_when_the_source_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control case: on the happy path nothing may be unloaded.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")

    assert processing.start("hw:physical", name="PeriferiaMic") == "echo-cancel-source"
    assert fake.unloaded == []


def test_start_publishes_the_requested_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Applications pick the mic by its description, so the name has to reach
    # PipeWire as source_properties.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")

    processing.start("hw:physical", name="PeriferiaMic")

    kind, args = fake.loaded[0]
    assert kind == "module-echo-cancel"
    assert "source=hw:physical" in args
    assert "source_properties=device.description=PeriferiaMic" in args


def test_start_still_names_the_source_when_processing_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With processing disabled the module earns its place only by providing a
    # stable name, but that is still what the daemon needs for PTT to work.
    off = ProcessingConfig(enabled=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=off)

    assert processing.start("hw:physical", name="PeriferiaMic") == "echo-cancel-source"
    _, args = fake.loaded[0]
    assert "source_properties=device.description=PeriferiaMic" in args
    assert not [a for a in args if a.startswith("aec_methods")], "aec must be off"
    assert not [a for a in args if a.startswith("noise_suppression")], "rnnoise must be off"


def test_start_without_a_name_and_without_processing_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = ProcessingConfig(enabled=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=off)

    assert processing.start("hw:physical") is None
    assert fake.loaded == []


def test_attach_binds_the_gate_and_starts_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(audio_mod, "pipewire", _FakePipewire(None))
    mic = VirtualMic(AudioConfig(attack_ms=2000), slider=lambda s, v: seen.append((s, v)))

    assert mic.attach("echo-cancel-source") == "echo-cancel-source"
    assert seen == [("echo-cancel-source", 0.0)], "the gate must start closed"


def test_attach_rejects_a_missing_source(monkeypatch: pytest.MonkeyPatch) -> None:
    mic = _mic(monkeypatch)

    assert mic.attach(None) is None
    assert mic.source is None


def test_teardown_does_not_unload_a_module_it_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MicProcessing owns the module now. Unloading it twice would make the
    # second unload hit an id PipeWire has already reused for something else.
    mic = _mic(monkeypatch)
    mic.attach("echo-cancel-source")

    mic.teardown()

    assert mic.source is None
