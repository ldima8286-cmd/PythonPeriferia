from __future__ import annotations

import importlib
from typing import Any

import pytest

# See the note in test_audio_ramp.py: importlib resolves these submodules
# reliably where the import statement does not.
config_mod = importlib.import_module("periferia.core.config")
audio_mod = importlib.import_module("periferia.modules.audio")
processing_mod = importlib.import_module("periferia.modules.processing")
pipewire_mod = importlib.import_module("periferia.core.pipewire")

AudioConfig = config_mod.AudioConfig
ProcessingConfig = config_mod.ProcessingConfig
VirtualMic = audio_mod.VirtualMic
MicProcessing = processing_mod.MicProcessing


class _FakePipewire:
    """Records module loads/unloads so a leak shows up as a test failure."""

    PipeWireError = pipewire_mod.PipeWireError

    def __init__(self, source: str | None) -> None:
        self.source = source
        self.loaded: list[tuple[str, list[str]]] = []
        self.unloaded: list[int] = []
        self.stale: list[int] = []
        self.stale_checked: list[tuple[str, str]] = []
        self.volumes: list[tuple[str, float]] = []
        self.source_for: dict[int, str] = {}
        self.fail_kinds: set[str] = set()
        self._next_id = 77
        self.default_src: str | None = None
        self.default_set: list[str] = []

    def find_source(self, name: str) -> str | None:
        return None

    def source_exists(self, name: str) -> bool:
        return False

    def default_source(self) -> str | None:
        return self.default_src

    def set_default_source(self, name: str) -> None:
        if "set-default-source" in self.fail_kinds:
            raise pipewire_mod.PipeWireError("set-default-source denied")
        self.default_set.append(name)
        self.default_src = name

    def source_by_module(self, module_id: int) -> str | None:
        return self.source_for.get(module_id, self.source)

    def set_volume(self, name: str, fraction: float) -> None:
        self.volumes.append((name, fraction))

    def load_module(self, kind: str, args: list[str]) -> int:
        if kind in self.fail_kinds:
            return None  # type: ignore[return-value]
        self.loaded.append((kind, args))
        self._next_id += 1
        return self._next_id - 1

    def unload_module(self, module_id: int) -> bool:
        self.unloaded.append(module_id)
        return True

    def unload_stale(self, module_name: str, description: str) -> list[int]:
        # mirrors the real one, which unloads through unload_module
        self.stale_checked.append((module_name, description))
        removed = []
        for module_id in self.stale:
            if self.unload_module(module_id):
                removed.append(module_id)
        return removed


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
    cfg = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=cfg)

    assert processing.start("hw:physical", name="PeriferiaMic") == "echo-cancel-source"
    assert fake.unloaded == []


def test_start_publishes_the_requested_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Applications pick the mic by its description, so the name has to reach
    # PipeWire as source_properties.
    cfg = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=cfg)

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
    off = ProcessingConfig(enabled=False, stereo_to_mono=False)
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


def test_stale_module_is_unloaded_before_loading_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A killed run leaves the module up and the new source collides on name, so
    # the gate drives one source while the self check reads the other. Both
    # module kinds are cleared, because a previous run under stereo_to_mono
    # leaves a remap carrying the very same name.
    legacy = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=legacy)
    fake.stale = [41, 42]

    processing.start("hw:physical", name="PeriferiaMic")

    assert fake.unloaded == [41, 42, 41, 42], "leftovers have to go before ours loads"
    assert fake.stale_checked == [
        ("module-remap-source", "PeriferiaMic"),
        ("module-echo-cancel", "PeriferiaMic"),
    ]


def test_no_stale_module_means_nothing_extra_is_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing, fake = _processing(
        monkeypatch,
        source="echo-cancel-source",
        cfg=ProcessingConfig(stereo_to_mono=False),
    )

    processing.start("hw:physical", name="PeriferiaMic")

    assert fake.unloaded == []


def test_mono_publishes_left_channel_as_a_single_mono_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The echo-cancel output is stereo however its input is wired, and its two
    # channels are not duplicates of one mono input, so an app downmixing them
    # can cancel a band. The remap on top publishes one mono channel fed by the
    # left channel of that stage, which no downmix can cancel.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    fake.source_for = {78: "PeriferiaMic"}

    assert processing.start("hw:physical", name="PeriferiaMic") == "PeriferiaMic"

    assert [kind for kind, _ in fake.loaded] == [
        "module-echo-cancel",
        "module-remap-source",
    ]

    ec_args = fake.loaded[0][1]
    assert "source=hw:physical" in ec_args
    assert "source_properties=device.description=PeriferiaMic-stage" in ec_args

    remap_args = fake.loaded[1][1]
    assert "master=echo-cancel-source" in remap_args
    assert "master_channel_map=front-left" in remap_args
    assert "channel_map=mono" in remap_args
    assert "channels=1" in remap_args
    assert "source_name=PeriferiaMic" in remap_args
    assert "source_properties=device.description=PeriferiaMic" in remap_args


def test_mono_pins_the_stage_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing else touches the echo-cancel stage once the gate drives the mono
    # source, so the stage must not attenuate what the remap captures.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")

    processing.start("hw:physical", name="PeriferiaMic")

    assert fake.volumes == [("echo-cancel-source", 1.0)]


def test_mono_clears_leftovers_of_both_module_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A run under stereo_to_mono=False leaves the echo-cancel carrying the
    # virtual name, a run under the default leaves an extra remap behind.
    # Either has to go before anything new loads, or two sources answer to
    # one name.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    fake.stale = [41, 42]

    processing.start("hw:physical", name="PeriferiaMic")

    assert fake.stale_checked == [
        ("module-remap-source", "PeriferiaMic"),
        ("module-echo-cancel", "PeriferiaMic"),
        ("module-echo-cancel", "PeriferiaMic-stage"),
    ]
    assert set(fake.unloaded) == {41, 42}


def test_mono_remap_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    fake.fail_kinds = {"module-remap-source"}

    assert processing.start("hw:physical", name="PeriferiaMic") is None
    assert [kind for kind, _ in fake.loaded] == ["module-echo-cancel"]
    assert fake.unloaded == [77], "the stage has to go down with the failed remap"


def test_mono_remap_that_creates_no_source_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing, fake = _processing(monkeypatch, source=None)
    fake.source_for = {77: "echo-cancel-source"}

    assert processing.start("hw:physical", name="PeriferiaMic") is None
    assert fake.unloaded == [78, 77], "remap first, then the stage it captures"


def test_mono_teardown_unloads_remap_before_the_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    processing.start("hw:physical", name="PeriferiaMic")
    fake.unloaded.clear()

    processing.stop()

    assert fake.unloaded == [78, 77], "the capturing remap has to go first"


def test_mono_needs_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # With nothing to publish into there is no reason to add a second module:
    # keep the single echo-cancel behaviour.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")

    assert processing.start("hw:physical") == "echo-cancel-source"
    assert [kind for kind, _ in fake.loaded] == ["module-echo-cancel"]


def test_mono_obeys_the_devices_mono_from(monkeypatch: pytest.MonkeyPatch) -> None:
    # The channel kept is a per-device decision: a card oriented the other way
    # around wants the right one. It travels through the config, not a module
    # constant.
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    fake.source_for = {78: "PeriferiaMic"}
    processing.configure(
        ProcessingConfig(stereo_to_mono=True, mono_from="front-right")
    )

    assert processing.start("hw:physical", name="PeriferiaMic") == "PeriferiaMic"
    remap_args = fake.loaded[1][1]
    assert "master_channel_map=front-right" in remap_args


def test_configure_takes_effect_on_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A device change does not rebuild anything by itself: the numbers are
    # adopted here, and the chain that reads them is rebuilt by the caller.
    processing, _ = _processing(monkeypatch, source="echo-cancel-source")
    other = ProcessingConfig(noise_suppression=False, stereo_to_mono=False)

    processing.configure(other)

    assert processing.cfg is other


def test_default_is_moved_to_the_requested_device_and_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # module-echo-cancel captures the default source and ignores source= in
    # this PipeWire build, so a capture device that is not the default has to
    # become the default for the moment the module goes up.
    cfg = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=cfg)
    fake.default_src = "alsa_input.pci-0000_00_1f.3.analog-stereo"

    assert processing.start("hw:physical", name="PeriferiaMic") == "echo-cancel-source"
    assert fake.default_set == [
        "hw:physical",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
    ]


def test_default_already_matching_is_not_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=cfg)
    fake.default_src = "hw:physical"

    processing.start("hw:physical", name="PeriferiaMic")

    assert fake.default_set == []


def test_default_move_failure_does_not_stop_the_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ProcessingConfig(stereo_to_mono=False)
    processing, fake = _processing(monkeypatch, source="echo-cancel-source", cfg=cfg)
    fake.default_src = "alsa_input.pci-0000_00_1f.3.analog-stereo"
    fake.fail_kinds = {"set-default-source"}

    assert processing.start("hw:physical", name="PeriferiaMic") == "echo-cancel-source"
    assert fake.default_set == []


def test_the_default_is_restored_even_when_the_module_refuses_to_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing, fake = _processing(
        monkeypatch,
        source="echo-cancel-source",
        cfg=ProcessingConfig(stereo_to_mono=False),
    )
    fake.default_src = "alsa_input.pci-0000_00_1f.3.analog-stereo"
    fake.fail_kinds = {"module-echo-cancel"}

    assert processing.start("hw:physical", name="PeriferiaMic") is None
    assert fake.default_set == [
        "hw:physical",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
    ]


def test_the_default_is_restored_after_the_mono_stage_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing, fake = _processing(monkeypatch, source="echo-cancel-source")
    fake.source_for = {78: "PeriferiaMic"}
    fake.default_src = "alsa_input.pci-0000_00_1f.3.analog-stereo"

    assert processing.start("hw:physical", name="PeriferiaMic") == "PeriferiaMic"
    assert fake.default_set == [
        "hw:physical",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
    ]


def test_stale_detection_ignores_other_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only our own leftovers, matched on the description we asked for, because
    # the source name is not the same on every machine.
    listed = [
        {
            "name": "echo-cancel-source",
            "owner_module": 7,
            "properties": {"device.description": "PeriferiaMic"},
        },
        {
            "name": "somebody-elses-mic",
            "owner_module": 9,
            "properties": {"device.description": "Zoom"},
        },
    ]
    # "list short modules" reports no id, so ownership is confirmed through
    # the description the module was asked to carry.
    modules = [
        {
            "name": "module-echo-cancel",
            "argument": "source=x source_properties=device.description=PeriferiaMic",
        },
        {
            "name": "module-echo-cancel",
            "argument": "source=y source_properties=device.description=Zoom",
        },
        {"name": "module-loopback", "argument": "source.z=1"},
    ]
    monkeypatch.setattr(pipewire_mod, "sources", lambda: listed)
    monkeypatch.setattr(pipewire_mod, "modules", lambda: modules)
    # These read through pactl, so the graph has to be out of the way.
    monkeypatch.setattr(pipewire_mod, "graph_module_ids", lambda _d: None)

    assert pipewire_mod.stale_modules("module-echo-cancel", "PeriferiaMic") == [7]
    assert pipewire_mod.stale_modules("module-echo-cancel", "") == []
    assert pipewire_mod.stale_modules("module-loopback", "PeriferiaMic") == []


def test_stale_modules_finds_a_leftover_without_a_module_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exactly what pactl returns: "list short modules" has no id field at all,
    # only name and argument. Matching owners against a missing id is what
    # made this silently find nothing, so cleanup never ran and a killed run
    # left its module up forever.
    monkeypatch.setattr(
        pipewire_mod,
        "sources",
        lambda: [
            {
                "name": "echo-cancel-source",
                "owner_module": 536870917,
                "properties": {
                    "device.description": "PeriferiaMic",
                    "node.name": "echo-cancel-source",
                },
            }
        ],
    )
    monkeypatch.setattr(
        pipewire_mod,
        "modules",
        lambda: [
            {"name": "libpipewire-module-rt", "argument": ""},
            {
                "name": "module-echo-cancel",
                "argument": (
                    "source=alsa_input.pci-0000_00_1f.3.analog-stereo "
                    "aec_methods=aec3 source_properties=device.description=PeriferiaMic"
                ),
            },
        ],
    )
    monkeypatch.setattr(pipewire_mod, "graph_module_ids", lambda _d: None)

    assert pipewire_mod.stale_modules("module-echo-cancel", "PeriferiaMic") == [536870917]


def test_stale_modules_ignores_a_module_that_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The owner has to be ours before it gets unloaded, or Periferia would
    # tear down somebody else's module.
    monkeypatch.setattr(
        pipewire_mod,
        "sources",
        lambda: [
            {
                "name": "echo-cancel-source",
                "owner_module": 42,
                "properties": {"device.description": "PeriferiaMic"},
            }
        ],
    )
    monkeypatch.setattr(
        pipewire_mod,
        "modules",
        lambda: [{"name": "module-echo-cancel", "argument": "source=whatever"}],
    )
    monkeypatch.setattr(pipewire_mod, "graph_module_ids", lambda _d: [42])

    assert pipewire_mod.stale_modules("module-echo-cancel", "PeriferiaMic") == []


def test_stale_modules_reads_the_graph_and_skips_pactl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The graph carries the pulse module id on the node itself, so the source
    # list never has to be read. pactl is only a fallback for when pw-dump is
    # missing or the daemon cannot be reached.
    def boom() -> list[dict[str, Any]]:
        raise AssertionError("sources() should not be called when the graph answers")

    monkeypatch.setattr(pipewire_mod, "sources", boom)
    monkeypatch.setattr(pipewire_mod, "graph_module_ids", lambda _d: [536870916])
    monkeypatch.setattr(
        pipewire_mod,
        "modules",
        lambda: [
            {
                "name": "module-echo-cancel",
                "argument": "source=x source_properties=device.description=PeriferiaMic",
            }
        ],
    )

    assert pipewire_mod.stale_modules("module-echo-cancel", "PeriferiaMic") == [536870916]


def test_stale_modules_waits_for_the_graph_to_be_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None means "the graph could not be read", which is the cue to fall back,
    # not an empty answer.
    monkeypatch.setattr(pipewire_mod, "graph_module_ids", lambda _d: None)
    monkeypatch.setattr(
        pipewire_mod,
        "sources",
        lambda: [
            {
                "name": "echo-cancel-source",
                "owner_module": 12,
                "properties": {"device.description": "PeriferiaMic"},
            }
        ],
    )
    monkeypatch.setattr(
        pipewire_mod,
        "modules",
        lambda: [
            {
                "name": "module-echo-cancel",
                "argument": "source_properties=device.description=PeriferiaMic",
            }
        ],
    )

    assert pipewire_mod.stale_modules("module-echo-cancel", "PeriferiaMic") == [12]


def test_gate_configure_swaps_the_device_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A device profile overrides the gate numbers key by key, so the gate has
    # to be able to adopt them without rebuilding anything.
    mic = _mic(monkeypatch)
    mic._physical = "old"
    other = AudioConfig(attack_ms=7, curve="linear")

    mic.configure(other)

    assert mic.cfg is other
    assert mic._physical is None, "re-resolve against the new settings"
