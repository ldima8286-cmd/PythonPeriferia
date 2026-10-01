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
    assert set(data) == {"audio", "ptt", "processing", "log", "macros", "profiles"}
    assert isinstance(data["audio"], dict)
    assert data["audio"]["attack_ms"] == 10
    assert data["profiles"] == []
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
