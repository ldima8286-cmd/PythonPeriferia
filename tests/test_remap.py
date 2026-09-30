"""Tests for the remap tables, which are pure logic and need no hardware."""

from __future__ import annotations

import pytest
from evdev import ecodes

from src.periferia.modules.remap import (
    Profile,
    RemapError,
    build_remap,
    held_modifier_codes,
    translate_events,
    translate_key,
)


def code(name: str) -> int:
    return ecodes.ecodes[name]


class TestBuildRemap:
    def test_empty_is_allowed(self):
        assert build_remap({}) == {}

    def test_named_keys_become_codes(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        assert table == {code("KEY_CAPSLOCK"): code("KEY_ESC")}

    def test_letters_are_accepted(self):
        table = build_remap({"q": "w"})
        assert table == {code("KEY_Q"): code("KEY_W")}

    def test_russian_letters_map_to_physical_keys(self):
        # In JCUKEN Й sits on the physical Q and Щ on the physical O, which is
        # the point: these are positions on the board, not letters.
        table = build_remap({"Й": "Щ"})
        assert table == {code("KEY_Q"): code("KEY_O")}

    def test_unknown_source_is_rejected(self):
        with pytest.raises(RemapError, match="unknown source key"):
            build_remap({"KEY_NOSUCHKEY": "KEY_A"})

    def test_unknown_target_names_the_source(self):
        with pytest.raises(RemapError, match="KEY_A maps to unknown target key"):
            build_remap({"KEY_A": "KEY_NOSUCHKEY"})

    def test_self_mapping_is_rejected(self):
        with pytest.raises(RemapError, match="maps to itself"):
            build_remap({"KEY_A": "KEY_A"})

    def test_modifier_source_is_rejected(self):
        with pytest.raises(RemapError, match="cannot be remapped yet"):
            build_remap({"KEY_LEFTSHIFT": "KEY_A"})

    def test_modifier_target_is_rejected(self):
        with pytest.raises(RemapError, match="stuck down"):
            build_remap({"KEY_A": "KEY_LEFTCTRL"})

    def test_held_modifier_set(self):
        codes = held_modifier_codes()
        assert code("KEY_LEFTSHIFT") in codes
        assert code("KEY_A") not in codes

    def test_lock_keys_are_remappable(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        assert table == {code("KEY_CAPSLOCK"): code("KEY_ESC")}


class TestTranslate:
    def test_mapped_key_changes_code(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        assert translate_key(code("KEY_CAPSLOCK"), table) == code("KEY_ESC")

    def test_unmapped_key_passes_through(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        assert translate_key(code("KEY_A"), table) == code("KEY_A")

    def test_empty_table_changes_nothing(self):
        assert translate_key(code("KEY_A"), {}) == code("KEY_A")

    def test_events_are_rewritten_in_order(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        events = [
            (ecodes.EV_KEY, code("KEY_CAPSLOCK"), 1),
            (ecodes.EV_KEY, code("KEY_CAPSLOCK"), 0),
        ]
        assert translate_events(events, table) == [
            (ecodes.EV_KEY, code("KEY_ESC"), 1),
            (ecodes.EV_KEY, code("KEY_ESC"), 0),
        ]

    def test_release_maps_like_the_press(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        out = translate_events([(ecodes.EV_KEY, code("KEY_CAPSLOCK"), 0)], table)
        assert out[0][1] == code("KEY_ESC")

    def test_other_event_types_are_untouched(self):
        table = build_remap({"KEY_CAPSLOCK": "KEY_ESC"})
        events = [
            (ecodes.EV_SYN, 0, 0),
            (ecodes.EV_MSC, code("MSC_SCAN"), 0x70065),
            (ecodes.EV_KEY, code("KEY_A"), 1),
        ]
        assert translate_events(events, table) == events

    def test_two_keys_may_share_a_target(self):
        table = build_remap({"KEY_A": "KEY_Z", "KEY_B": "KEY_Z"})
        assert table[code("KEY_A")] == table[code("KEY_B")] == code("KEY_Z")

    def test_count_is_preserved(self):
        table = build_remap({"KEY_A": "KEY_B", "KEY_C": "KEY_D"})
        events = [(ecodes.EV_KEY, code(n), 1) for n in ("KEY_A", "KEY_C", "KEY_F")]
        assert len(translate_events(events, table)) == 3


class TestProfile:
    def test_describe_empty(self):
        assert Profile("default", {}).describe() == "default: no remapping"

    def test_describe_names_both_ends(self):
        profile = Profile("game", build_remap({"KEY_CAPSLOCK": "KEY_ESC"}))
        text = profile.describe()
        assert "game" in text
        assert "CAPSLOCK" in text
        assert "ESC" in text

    def test_conflicts_reports_listened_keys(self):
        profile = Profile("game", build_remap({"KEY_CAPSLOCK": "KEY_ESC"}))
        assert profile.conflicts_with(code("KEY_A"), code("KEY_CAPSLOCK")) == [
            code("KEY_CAPSLOCK")
        ]
        assert profile.conflicts_with(code("KEY_A")) == []
