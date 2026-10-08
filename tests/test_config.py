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


class TestValidation:
    def test_wrong_type_is_named_and_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  attack_ms: fast\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"audio\.attack_ms: expected a whole number"):
            config_mod.load(path)

    def test_a_string_does_not_pass_for_a_number(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  target_volume: loud\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"audio\.target_volume: expected a number"):
            config_mod.load(path)

    def test_a_number_does_not_pass_for_text(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ptt:\n  ptt_key: 12\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"ptt\.ptt_key: expected text"):
            config_mod.load(path)

    def test_a_string_does_not_pass_for_a_boolean(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  enabled: 'true'\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"audio\.enabled: expected true or false"):
            config_mod.load(path)

    def test_negative_time_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  hold_ms: -5\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"audio\.hold_ms: must not be negative"):
            config_mod.load(path)

    def test_unknown_curve_name_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  curve: straight\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"audio\.curve: expected one of exp, linear, s_curve"):
            config_mod.load(path)

    def test_unknown_log_level_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("log:\n  level: chatty\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"log\.level: expected one of"):
            config_mod.load(path)

    def test_extra_props_values_must_be_text(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("processing:\n  extra_props:\n    node.name: null\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"processing\.extra_props\.node\.name: expected text"):
            config_mod.load(path)

    def test_a_wrong_device_override_value_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "devices:\n  - audio:\n      target_volume: loud\n", encoding="utf-8"
        )
        with pytest.raises(
            ValueError, match=r"devices\[0\]\.audio\.target_volume: expected a number"
        ):
            config_mod.load(path)

    def test_a_wrong_device_match_value_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "devices:\n  - match:\n      device.bus: 7\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match=r"DeviceProfile\.match\.device\.bus: expected text"):
            config_mod.load(path)

    def test_remap_values_must_be_text(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: 5\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=r"remap\.KEY_CAPSLOCK: expected text"):
            config_mod.load(path)

    def test_macro_step_hold_must_be_a_whole_number(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "macros:\n"
            "  - name: jump\n"
            "    bind: KEY_SPACE\n"
            "    steps:\n"
            "      - key: KEY_SPACE\n"
            "        hold_ms: brief\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=r"steps\[0\]\.hold_ms: expected a whole number"):
            config_mod.load(path)

    def test_pointer_speed_accepts_a_number(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n  - name: precise\n    pointer:\n      speed: 1.5\n",
            encoding="utf-8",
        )
        assert config_mod.load(path).profiles[0].pointer.speed == 1.5

    def test_valid_values_still_pass(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "audio:\n"
            "  attack_ms: 10\n"
            "  target_volume: 1.4\n"
            "  curve: s_curve\n"
            "  enabled: true\n",
            encoding="utf-8",
        )
        cfg = config_mod.load(path)
        assert cfg.audio.attack_ms == 10
        assert cfg.audio.target_volume == 1.4
        assert cfg.audio.curve == "s_curve"

    def test_missing_file_is_still_a_file_error_not_a_validation_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            config_mod.load(tmp_path / "absent.yaml")

    def test_an_empty_match_means_fallback_and_is_accepted(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "devices:\n"
            "  - name: fallback\n"
            "    match:\n"
            "    audio:\n"
            "      target_volume: 0.7\n"
            "profiles:\n"
            "  - name: default\n"
            "    match:\n"
            "    remap:\n",
            encoding="utf-8",
        )
        cfg = config_mod.load(path)
        assert cfg.devices[0].match == {}
        assert cfg.profiles[0].match == {}


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
