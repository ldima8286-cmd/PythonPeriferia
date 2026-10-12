"""Exports, merges and imports of the profile set."""

from __future__ import annotations

import pytest

from periferia.core import config as config_mod
from periferia.core import profiles as profiles_mod

TEXT = """\
profiles:
  - name: games
    enabled: true
    match:
      class:
        - game.exe
    remap:
      KEY_CAPSLOCK: KEY_LEFTSHIFT
    pointer:
      speed: 1.5
    macros:
      - name: combo
        bind: KEY_F5
        enabled: true

  - name: fallback
    remap:
      KEY_RIGHTCTRL: "off"
"""


def _write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_export_holds_only_the_profiles(tmp_path):
    conf = config_mod.load(_write_config(tmp_path, TEXT))
    block = profiles_mod.export_block(conf)
    assert set(block) == {"profiles"}
    assert len(block["profiles"]) == 2
    assert block["profiles"][0]["name"] == "games"
    assert block["profiles"][0]["pointer"] == {"speed": 1.5}

    fallback = block["profiles"][1]
    for default_key in ("enabled", "match", "pointer", "macros"):
        assert default_key not in fallback
    assert fallback["remap"] == {"KEY_RIGHTCTRL": "off"}


def test_parse_accepts_a_block_and_a_bare_list():
    bare = TEXT.partition("profiles:\n")[2]
    from_block = profiles_mod.parse_text(TEXT)
    from_bare = profiles_mod.parse_text(bare)
    assert from_block == from_bare
    assert [p.name for p in from_block] == ["games", "fallback"]
    assert from_block[0].pointer is not None
    assert from_block[0].pointer.speed == 1.5
    assert from_block[0].macros[0].bind == "KEY_F5"


def test_parse_refuses_unknown_keys():
    with pytest.raises(ValueError):
        profiles_mod.parse_text("- name: x\n  volume: 9\n")


def test_merge_replaces_same_name_and_appends_new(tmp_path):
    conf = config_mod.load(
        _write_config(
            tmp_path,
            "profiles:\n"
            "- name: fallback\n"
            "  remap:\n"
            "    KEY_A: KEY_B\n"
            "- name: apps\n",
        )
    )
    incoming = profiles_mod.parse_text(
        "- name: games\n  remap:\n    KEY_A: KEY_C\n"
        "- name: fallback\n  remap:\n    KEY_A: KEY_D\n"
    )
    merged = profiles_mod.merge(conf, incoming)
    assert [p.name for p in merged.profiles] == ["fallback", "apps", "games"]
    assert merged.profiles[0].remap == {"KEY_A": "KEY_D"}


def test_merge_replace_drops_the_kept_profiles(tmp_path):
    conf = config_mod.load(
        _write_config(
            tmp_path,
            "profiles:\n- name: fallback\n- name: apps\n",
        )
    )
    incoming = profiles_mod.parse_text(
        "- name: games\n- name: fallback\n"
    )
    merged = profiles_mod.merge(conf, incoming, replace=True)
    assert [p.name for p in merged.profiles] == ["games", "fallback"]


def test_round_trip_through_a_file(tmp_path):
    conf = config_mod.load(_write_config(tmp_path, TEXT))
    export = tmp_path / "profiles.yaml"
    profiles_mod.write_raw(export, profiles_mod.export_block(conf))
    assert profiles_mod.parse_file(export) == conf.profiles