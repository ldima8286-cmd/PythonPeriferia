"""Tests for the remap tables, which are pure logic and need no hardware."""

from __future__ import annotations

import pytest
from evdev import ecodes

from src.periferia.modules.remap import (
    Profile,
    RemapError,
    active_remap,
    build_remap,
    held_modifier_codes,
    translate_events,
    translate_key,
)
from src.periferia.modules.router import plan


def code(name: str) -> int:
    return ecodes.ecodes[name]


class _Entry:
    """Stands in for ProfileConfig without importing the config module."""

    def __init__(self, enabled: bool, remap: dict[str, str]) -> None:
        self.enabled = enabled
        self.remap = remap


def _profile(remap: dict[str, str]) -> _Entry:
    return _Entry(True, remap)


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


class TestActiveRemap:
    def test_no_profiles_means_no_table(self):
        from src.periferia.modules.remap import active_remap

        assert active_remap([]) is None

    def test_disabled_profile_is_skipped(self):
        from src.periferia.modules.remap import active_remap

        entries = [_Entry(enabled=False, remap={"KEY_A": "KEY_B"})]
        assert active_remap(entries) is None

    def test_empty_profile_is_skipped(self):
        from src.periferia.modules.remap import active_remap

        assert active_remap([_Entry(enabled=True, remap={})]) is None

    def test_first_enabled_profile_wins(self):
        from src.periferia.modules.remap import active_remap

        entries = [
            _Entry(enabled=False, remap={"KEY_A": "KEY_B"}),
            _Entry(enabled=True, remap={"KEY_C": "KEY_D"}),
            _Entry(enabled=True, remap={"KEY_E": "KEY_F"}),
        ]
        table = active_remap(entries)
        assert table == {code("KEY_C"): code("KEY_D")}

    def test_invalid_table_raises_rather_than_returning_empty(self):
        from src.periferia.modules.remap import active_remap

        with pytest.raises(RemapError):
            active_remap([_Entry(enabled=True, remap={"KEY_A": "KEY_LEFTSHIFT"})])


class TestDisablingAKey:
    """A key a profile turns off has to be gone, not renamed.

    Nothing about a swallowed key announces itself, so the only place a mistake
    here is visible is the key that still works when it should not.
    """

    def test_none_means_the_key_emits_nothing(self) -> None:
        caps = code("KEY_CAPSLOCK")
        table = build_remap({"KEY_CAPSLOCK": "none"})
        assert table == {caps: None}

    @pytest.mark.parametrize("written", ["none", "NONE", " off ", "disable", "-"])
    def test_every_spelling_of_off_means_the_same(self, written: str) -> None:
        caps = code("KEY_CAPSLOCK")
        assert build_remap({"KEY_CAPSLOCK": written}) == {caps: None}

    def test_a_disabled_event_is_left_out_entirely(self) -> None:
        caps = code("KEY_CAPSLOCK")
        events = [(ecodes.EV_KEY, caps, 1), (ecodes.EV_KEY, caps, 0)]
        assert translate_events(events, {caps: None}) == []

    def test_other_keys_still_come_through(self) -> None:
        caps = code("KEY_CAPSLOCK")
        esc = code("KEY_ESC")
        events = [(ecodes.EV_KEY, caps, 1), (ecodes.EV_KEY, esc, 1)]
        assert translate_events(events, {caps: None}) == [(ecodes.EV_KEY, esc, 1)]

    def test_synch_survives_a_disabled_key(self) -> None:
        caps = code("KEY_CAPSLOCK")
        events = [(ecodes.EV_SYN, 0, 0), (ecodes.EV_KEY, caps, 1)]
        assert translate_events(events, {caps: None}) == [(ecodes.EV_SYN, 0, 0)]

    def test_a_disabled_key_does_not_trigger_a_macro(self) -> None:
        """Otherwise the profile does one thing to the desktop and another to
        us, and only a game with a hotkey on it would ever say so."""
        f5 = code("KEY_F5")
        caps = code("KEY_CAPSLOCK")
        watch = frozenset({f5, caps})
        forwarded, actions = plan([(ecodes.EV_KEY, f5, 1)], {f5: None}, watch)
        assert forwarded == []
        assert actions == []

    def test_a_disabled_key_does_not_hold_push_to_talk_open(self) -> None:
        grave = code("KEY_GRAVE")
        events = [(ecodes.EV_KEY, grave, 1)]
        _, actions = plan(events, {grave: None}, frozenset({grave}))
        assert actions == []

    def test_disabling_and_remapping_coexist(self) -> None:
        caps = code("KEY_CAPSLOCK")
        f1 = code("KEY_F1")
        table = build_remap({"KEY_CAPSLOCK": "none", "KEY_F1": "KEY_F13"})
        assert table == {caps: None, f1: code("KEY_F13")}

    def test_a_disabled_modifier_is_allowed_because_it_emits_nothing(self) -> None:
        """The reason modifiers are refused is that emitting one can leave it
        held down. Emitting nothing cannot."""
        table = build_remap({"KEY_LEFTSHIFT": "none"})
        assert table == {code("KEY_LEFTSHIFT"): None}

    def test_a_misspelled_off_is_an_error_rather_than_a_dead_key(self) -> None:
        """Writing "nof" instead of "off" must not quietly disable a key.
        There is no key by that name, so it is reported like any other typo."""
        with pytest.raises(RemapError, match="unknown target key"):
            build_remap({"KEY_CAPSLOCK": "nof"})

    def test_a_table_of_only_disabled_keys_still_counts_as_remapping(self) -> None:
        assert active_remap([_profile({"KEY_CAPSLOCK": "none"})]) == {
            code("KEY_CAPSLOCK"): None
        }
