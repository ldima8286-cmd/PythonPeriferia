from __future__ import annotations

import dataclasses

import pytest

from periferia.core import config as config_mod


def test_explicit_missing_path_raises() -> None:
    with pytest.raises(FileNotFoundError):
        config_mod.load("/nonexistent/config.yaml")


def test_defaults_when_nothing_configured() -> None:
    cfg = config_mod.load()
    assert cfg.path is None
    assert cfg.audio.virtual_name == "PeriferiaMic"
    assert cfg.ptt.panic_key == "KEY_F12"
    assert cfg.processing.noise_suppression is True


def test_to_dict_is_nested_plain_data() -> None:
    data = config_mod.Config().to_dict()
    assert set(data) == {"audio", "ptt", "processing", "log", "macros", "profiles", "devices"}
    assert isinstance(data["audio"], dict)
    assert data["audio"]["attack_ms"] == 10
    assert data["profiles"] == []
    assert data["devices"] == []
    assert data["macros"] == []


def test_build_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        config_mod._build(config_mod.AudioConfig, {"attack_msec": 5})


def test_build_applies_enabled_toggle() -> None:
    cfg = config_mod._build(config_mod.AudioConfig, {"enabled": False})
    assert cfg.enabled is False


def test_build_on_none_returns_defaults() -> None:
    assert config_mod._build(config_mod.PttConfig, None) == config_mod.PttConfig()


def test_every_field_is_typed() -> None:
    for cls in (
        config_mod.AudioConfig,
        config_mod.PttConfig,
        config_mod.ProcessingConfig,
        config_mod.LogConfig,
    ):
        assert dataclasses.is_dataclass(cls)


def test_curve_names_are_known() -> None:
    valid = {"exp", "linear", "s_curve"}
    assert config_mod.AudioConfig().curve in valid


class TestDevices:
    def test_absent_gives_no_devices(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  target_volume: 1.0\n", encoding="utf-8")
        assert config_mod.load(path).devices == []

    def test_devices_are_parsed(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "devices:\n"
            "  - name: usb\n"
            "    match:\n"
            "      device.bus: usb\n"
            "    processing:\n"
            "      echo_cancellation: false\n"
            "      stereo_to_mono: false\n",
            encoding="utf-8",
        )
        devices = config_mod.load(path).devices
        assert len(devices) == 1
        assert devices[0].name == "usb"
        assert devices[0].enabled is True
        assert devices[0].match == {"device.bus": "usb"}
        assert devices[0].processing == {"echo_cancellation": False, "stereo_to_mono": False}

    def test_not_a_list_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("devices:\n  name: usb\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected a list for devices"):
            config_mod.load(path)

    def test_unknown_device_key_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("devices:\n  - nope: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"devices\[0\]: unknown keys"):
            config_mod.load(path)

    def test_unknown_override_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("devices:\n  - audio:\n      volume: 0.5\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"devices\[0\].audio: unknown keys"):
            config_mod.load(path)

    def test_override_must_be_a_mapping(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("devices:\n  - audio: loud\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"devices\[0\].audio: expected a mapping"):
            config_mod.load(path)

    def test_first_match_wins(self):
        cfg = config_mod.Config(
            devices=[
                config_mod.DeviceProfile(name="usb", match={"device.bus": "usb"}),
                config_mod.DeviceProfile(name="fallback"),
            ]
        )
        props = {"device.bus": "usb", "node.name": "x"}
        assert config_mod.match_profile(cfg, props).name == "usb"
        assert config_mod.match_profile(cfg, {}).name == "fallback"

    def test_a_disabled_entry_is_skipped(self):
        cfg = config_mod.Config(
            devices=[
                config_mod.DeviceProfile(name="off", match={}, enabled=False),
                config_mod.DeviceProfile(name="on", match={}),
            ]
        )
        assert config_mod.match_profile(cfg, {}).name == "on"

    def test_every_rule_has_to_agree(self):
        cfg = config_mod.Config(
            devices=[
                config_mod.DeviceProfile(
                    name="exact", match={"device.bus": "usb", "node.name": "headset"}
                )
            ]
        )
        assert config_mod.match_profile(cfg, {"device.bus": "usb", "node.name": "other"}) is None
        assert (
            config_mod.match_profile(cfg, {"device.bus": "usb", "node.name": "headset"}) is not None
        )

    def test_effective_overlays_only_written_keys(self):
        cfg = config_mod.Config()
        cfg.audio.target_volume = 0.5
        cfg.processing.stereo_to_mono = True
        profile = config_mod.DeviceProfile(
            audio={"target_volume": 1.0}, processing={"stereo_to_mono": False}
        )
        audio, processing = config_mod.effective(cfg, profile)
        assert audio.target_volume == 1.0
        assert audio.attack_ms == cfg.audio.attack_ms, "an absent key keeps the base value"
        assert processing.stereo_to_mono is False
        assert processing.mono_from == cfg.processing.mono_from

    def test_effective_without_profile_keeps_the_sections(self):
        cfg = config_mod.Config()
        cfg.audio.target_volume = 0.7
        audio, processing = config_mod.effective(cfg, None)
        assert audio.target_volume == 0.7
        assert processing is cfg.processing

    def test_props_match_compares_as_strings(self):
        cfg = config_mod.Config(
            devices=[config_mod.DeviceProfile(name="card0", match={"api.alsa.card": "0"})]
        )
        assert (
            config_mod.match_profile(cfg, {"api.alsa.card": 0}).name == "card0"
        ), "a number has to match the string spelling"


class TestProfiles:
    def test_absent_gives_no_profiles(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ptt:\n  ptt_key: KEY_GRAVE\n", encoding="utf-8")
        assert config_mod.load(path).profiles == []

    def test_profiles_are_parsed(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n"
            "  - name: game\n"
            "    remap:\n"
            "      KEY_CAPSLOCK: KEY_ESC\n",
            encoding="utf-8",
        )
        profiles = config_mod.load(path).profiles
        assert len(profiles) == 1
        assert profiles[0].name == "game"
        assert profiles[0].enabled is True
        assert profiles[0].remap == {"KEY_CAPSLOCK": "KEY_ESC"}

    def test_not_a_list_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("profiles:\n  name: game\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected a list for profiles"):
            config_mod.load(path)

    def test_unknown_profile_key_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("profiles:\n  - name: game\n    nope: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"profiles\[0\]: unknown keys"):
            config_mod.load(path)

    def test_to_dict_roundtrips(self):
        cfg = config_mod.Config(profiles=[config_mod.ProfileConfig(name="game")])
        assert cfg.to_dict()["profiles"][0]["name"] == "game"
