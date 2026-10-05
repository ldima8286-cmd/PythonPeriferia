"""Checks for configs that would fail silently instead of loudly."""

from __future__ import annotations

import pytest

from periferia.core import config as config_mod
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
                _profile("rest", remap={"KEY_S": "KEY_D"}),
            ]
        )
        assert not any("shadow" in p.message for p in report.problems)
        assert not report.problems

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


def test_says_when_no_profile_catches_the_other_windows() -> None:
    report = validate.check_profiles(
        [_profile("game", match={"resource_class": "steam"}, remap={"KEY_A": "KEY_B"})]
    )
    assert any("gets no remap at all" in p.message for p in report.problems)


def test_a_disabled_fallback_does_not_count_as_a_fallback() -> None:
    report = validate.check_profiles(
        [
            _profile("game", match={"resource_class": "steam"}, remap={"KEY_A": "KEY_B"}),
            _profile("rest", enabled=False, remap={"KEY_C": "KEY_D"}),
        ]
    )
    assert any("gets no remap at all" in p.message for p in report.problems)


class TestCheckMacros:
    """The macro validator.

    Everything here is about catching a macro that looks fine in the file and
    does nothing on the keyboard, which is the failure nobody notices until they
    press the key in a match.
    """

    def _macro(self, name="m", bind="KEY_F5", keys=("KEY_H",), **kw):
        return config_mod.MacroConfig(
            name=name,
            bind=bind,
            steps=[
                config_mod.MacroStep(key=k, at_ms=kw.get("gap", 0), hold_ms=kw.get("hold", 40))
                for k in keys
            ],
            **({"enabled": kw["enabled"]} if "enabled" in kw else {}),
        )

    def _check(self, macros, profiles=(), reserved=("KEY_F12", "KEY_GRAVE")):
        report = validate.check_macros(list(macros), list(profiles), reserved=list(reserved))
        return [p for p in report.problems if p.level == validate.ERROR]

    def test_a_good_macro_has_no_problems(self):
        assert self._check([self._macro()]) == []

    def test_nothing_configured_is_fine(self):
        assert self._check([]) == []

    def test_an_unknown_key_name_is_an_error(self):
        errors = self._check([self._macro(keys=("KEY_MUHLE_FN",))])
        assert len(errors) == 1
        assert "KEY_MUHLE_FN" in errors[0].message

    def test_an_unknown_bind_name_is_an_error(self):
        assert self._check([self._macro(bind="KEY_NOPE")])

    def test_a_macro_bound_to_a_reserved_key_is_refused(self):
        """It would fire while the panic was busy stopping things."""
        errors = self._check([self._macro(bind="KEY_F12")])
        assert errors
        assert "KEY_F12" in errors[0].message

    def test_a_macro_that_plays_a_reserved_key_is_refused(self):
        """Otherwise the macro would open the microphone every time it played."""
        assert self._check([self._macro(keys=("KEY_GRAVE",))])

    def test_two_macros_on_the_same_key_are_reported(self):
        errors = self._check([self._macro(name="one"), self._macro(name="two")])
        assert len(errors) == 1
        # The offending macro is named in 'where', the one it collides with in
        # the message, so both have to appear for the report to be actionable.
        assert "two" in errors[0].where
        assert "one" in errors[0].message
        assert "KEY_F5" in errors[0].message

    def test_two_macros_on_different_keys_are_fine(self):
        assert self._check([self._macro(name="one"), self._macro(name="two", bind="KEY_F6")]) == []

    def test_a_macro_with_no_steps_is_a_warning_not_an_error(self):
        report = validate.check_macros([self._macro(keys=())], [], reserved=["KEY_F12"])
        assert [p for p in report.problems if p.level == validate.ERROR] == []
        assert any(p.level == validate.WARNING for p in report.problems)

    def test_a_macro_on_no_key_is_a_warning_not_an_error(self):
        """It is kept and can still be played by name."""
        report = validate.check_macros([self._macro(bind="")], [], reserved=["KEY_F12"])
        assert [p for p in report.problems if p.level == validate.ERROR] == []
        assert any(p.level == validate.WARNING for p in report.problems)

    def test_a_profile_may_override_the_global_macro_of_the_same_name(self):
        """The whole point of per-profile macros. Flagging this as a duplicate
        would make the feature impossible to use."""
        profile = config_mod.ProfileConfig(
            name="game",
            enabled=True,
            macros=[self._macro(name="m", bind="KEY_F6", keys=("KEY_I",))],
        )
        assert self._check([self._macro(name="m")], [profile]) == []

    def test_two_macros_of_one_name_inside_a_profile_are_reported(self):
        profile = config_mod.ProfileConfig(
            name="game",
            enabled=True,
            macros=[self._macro(name="a"), self._macro(name="a", bind="KEY_F6")],
        )
        assert self._check([], [profile])

    def test_a_disabled_macro_is_not_reported(self):
        """It will not play, so complaining about its key wastes the report."""
        assert self._check([self._macro(enabled=False)]) == []

    def test_an_empty_name_is_reported(self):
        assert self._check([self._macro(name="")])

    def test_the_report_knows_whether_anything_will_work(self):
        report = validate.check_macros([self._macro(), self._macro(name="b", bind="KEY_NOPE")], [])
        assert report.ok is False
        assert validate.check_macros([self._macro()], []).ok is True


class TestTurningAKeyOff:
    """A key the profile turns off, and a target that only looks like one.

    The dangerous case is a misspelling: "0ff" is not a key, but it looks like
    it means "off", and a config that quietly disables the wrong key produces a
    keyboard with a hole in it and nothing in the log.
    """

    def test_none_is_not_a_typo(self) -> None:
        report = validate.check_profiles([_profile(remap={"KEY_CAPSLOCK": "none"})])
        assert [str(p) for p in report.errors] == []

    @pytest.mark.parametrize("written", ["0ff", "nof", "disble", "offf", "nope"])
    def test_a_misspelled_off_is_reported_as_an_unknown_key(self, written: str) -> None:
        report = validate.check_profiles([_profile(remap={"KEY_CAPSLOCK": written})])
        assert len(report.errors) == 1
        assert "To turn a key off write none" in str(report.errors[0])

    def test_a_real_target_is_still_fine(self) -> None:
        report = validate.check_profiles([_profile(remap={"KEY_CAPSLOCK": "KEY_ESC"})])
        assert [str(p) for p in report.errors] == []
