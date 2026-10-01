"""Checks for configs that would fail silently instead of loudly."""

from __future__ import annotations

import pytest

from periferia.core import validate
from periferia.core.config import ProfileConfig


def _profile(name: str = "p", **kwargs: object) -> ProfileConfig:
    return ProfileConfig(name=name, **kwargs)  # type: ignore[arg-type]


class TestTheTypoThatMatters:
    def test_a_misspelled_criterion_is_an_error(self) -> None:
        report = validate.check_profiles(
            [_profile("typo", match={"clas": "steam"})]  # type: ignore[dict-item]
        )
        assert not report.ok
        assert "never fire" in report.errors[0].message

    def test_it_says_what_the_real_fields_are(self) -> None:
        report = validate.check_profiles(
            [_profile("typo", match={"clas": "steam"})]  # type: ignore[dict-item]
        )
        assert "resource_class" in report.errors[0].message

    def test_the_same_criterion_is_accepted_by_the_matcher(self) -> None:
        """The validator exists because the matcher cannot object to this."""
        from periferia.core import windowprofile

        good = ProfileConfig(name="good", match={"resource_class": "steam"})
        assert windowprofile.matches(good, windowprofile.Window(resource_class="steam"))
        assert validate.check_profiles([good]).ok


class TestCaption:
    def test_matching_on_caption_is_refused(self) -> None:
        report = validate.check_profiles(
            [_profile("cap", match={"caption": "Steam"})]  # type: ignore[dict-item]
        )
        assert not report.ok
        assert "contents" in report.errors[0].message


class TestNames:
    def test_two_profiles_with_one_name_is_an_error(self) -> None:
        report = validate.check_profiles(
            [
                _profile("game", match={"resource_class": "steam"}),
                _profile("game", match={"resource_class": "lutris"}),
            ]
        )
        assert any("twice" in p.message for p in report.errors)

    def test_different_names_are_fine(self) -> None:
        report = validate.check_profiles(
            [
                _profile("a", match={"resource_class": "steam"}),
                _profile("b", match={"resource_class": "lutris"}),
            ]
        )
        assert report.ok


class TestReachability:
    def test_a_shadowed_profile_is_a_warning(self) -> None:
        report = validate.check_profiles(
            [
                _profile("first", match={"resource_class": "steam"}),
                _profile("second", match={"resource_class": "steam"}),
            ]
        )
        assert report.ok
        assert any("never be chosen" in p.message for p in report.warnings)

    def test_the_order_is_not_reported_as_shadowing(self) -> None:
        report = validate.check_profiles(
            [
                _profile("a", match={"resource_class": "steam"}, remap={"KEY_M": "KEY_N"}),
                _profile("b", match={"resource_class": "lutris"}, remap={"KEY_P": "KEY_Q"}),
            ]
        )
        assert not report.warnings

    def test_a_disabled_matched_profile_is_a_warning(self) -> None:
        report = validate.check_profiles(
            [_profile("off", match={"resource_class": "steam"}, enabled=False)]
        )
        assert any("disabled" in p.message for p in report.warnings)

    def test_profiles_that_all_match_nothing_are_flagged(self) -> None:
        report = validate.check_profiles([_profile("a"), _profile("b")])
        assert any("never used" in p.message for p in report.warnings)

    def test_no_profiles_at_all_is_reported(self) -> None:
        assert any("no profiles" in p.message for p in validate.check_profiles([]).warnings)


class TestReservedKeys:
    def test_remapping_the_panic_key_is_an_error(self) -> None:
        report = validate.check_profiles(
            [_profile("g", remap={"KEY_F12": "KEY_A"})],
            reserved=["KEY_F12"],
        )
        assert not report.ok
        assert "lose the way out" in report.errors[0].message

    def test_disabling_the_ptt_key_is_an_error(self) -> None:
        report = validate.check_profiles(
            [_profile("g", remap={"KEY_GRAVE": "none"})],
            reserved=["KEY_GRAVE"],
        )
        assert not report.ok

    def test_mapping_onto_a_reserved_key_is_only_a_warning(self) -> None:
        report = validate.check_profiles(
            [_profile("g", remap={"KEY_M": "KEY_F12"})],
            reserved=["KEY_F12"],
        )
        assert report.ok
        assert any("reserved" in p.message for p in report.warnings)

    def test_reserved_check_is_off_when_nothing_is_reserved(self) -> None:
        report = validate.check_profiles([_profile("g", remap={"KEY_F12": "KEY_A"})])
        assert report.ok


class TestRescueKeys:
    @pytest.mark.parametrize("key", ["KEY_ESC", "KEY_TAB", "KEY_LEFTMETA"])
    def test_disabling_a_way_out_is_an_error(self, key: str) -> None:
        report = validate.check_profiles([_profile("g", remap={key: "none"})])
        assert not report.ok
        assert "stays dead" in report.errors[0].message

    def test_remapping_escape_to_something_real_is_fine(self) -> None:
        report = validate.check_profiles([_profile("g", remap={"KEY_ESC": "KEY_M"})])
        assert report.ok


class TestSpellingOfDisable:
    @pytest.mark.parametrize("spelling", ["none", "NONE", " off ", "-"])
    def test_the_usual_spellings_all_mean_disabled(self, spelling: str) -> None:
        report = validate.check_profiles([_profile("g", remap={"KEY_ESC": spelling})])
        assert not report.ok

    def test_a_target_named_none_is_still_checked_as_a_disable(self) -> None:
        from periferia.core import validate as v

        assert v._is_disabled("none") and v._is_disabled("OFF")


class TestKeysTheMachineLacks:
    def test_a_key_no_keyboard_has_is_a_warning(self) -> None:
        report = validate.check_profiles(
            [_profile("g", remap={"KEY_A": "KEY_B"})], present_codes=frozenset()
        )
        assert any("cannot be pressed" in p.message for p in report.warnings)

    def test_a_key_name_nobody_has_is_an_error(self) -> None:
        report = validate.check_profiles([_profile("g", remap={"KEY_MUHLE_FN": "KEY_A"})])
        assert not report.ok
        assert "not a key this project knows" in report.errors[0].message

    def test_a_key_that_is_present_says_nothing(self) -> None:
        from periferia.modules.hotkey import resolve_key

        code = resolve_key("KEY_A")
        report = validate.check_profiles(
            [_profile("g", remap={"KEY_A": "KEY_B"})],
            present_codes=frozenset({code} if code is not None else set()),
        )
        assert not [p for p in report.warnings if "cannot be pressed" in p.message]

    def test_nothing_is_said_when_devices_were_not_looked_at(self) -> None:
        report = validate.check_profiles([_profile("g", remap={"KEY_A": "KEY_B"})])
        assert not [p for p in report.warnings if "cannot be pressed" in p.message]


class TestQuietProfiles:
    def test_a_profile_that_switches_but_changes_nothing_is_flagged(self) -> None:
        report = validate.check_profiles([_profile("g", match={"resource_class": "steam"})])
        assert any("changes no keys" in p.message for p in report.warnings)


class TestEmptyList:
    def test_an_empty_list_matches_nothing(self) -> None:
        report = validate.check_profiles(
            [_profile("g", match={"resource_class": []})]  # type: ignore[dict-item]
        )
        assert not report.ok
        assert "empty list" in report.errors[0].message


class TestReportShape:
    def test_it_prints_something_a_person_can_read(self) -> None:
        report = validate.check_profiles([_profile("typo", match={"clas": "steam"})])  # type: ignore[dict-item]
        assert "error" in str(report.problems[0])
        assert "profile 'typo'" in str(report.problems[0])

    def test_a_clean_config_has_nothing_to_say(self) -> None:
        report = validate.check_profiles(
            [
                _profile("game", match={"resource_class": "steam"}, remap={"KEY_M": "KEY_N"}),
                _profile("default"),
            ]
        )
        assert report.ok
        assert not report.warnings
