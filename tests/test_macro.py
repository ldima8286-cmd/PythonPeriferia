"""Macro rules that have nothing to do with a keyboard.

The interesting properties of a macro system are all refusals: it will not play
its own trigger, it will not wait an hour, it will not replay a key it does not
know. Those are cheap to test here and expensive to discover on a real keyboard.
"""

from __future__ import annotations

import pytest

from periferia.core import macro as macro_mod
from periferia.core.config import MacroConfig, MacroStep, ProfileConfig

CODES = {"KEY_A": 30, "KEY_B": 48, "KEY_C": 46, "KEY_LEFTCTRL": 29, "KEY_F5": 63}


def _resolve(name: str) -> int | None:
    return CODES.get(name)


def _macro(steps: list[tuple[int, int, int]], name: str = "m", bind: str = "") -> macro_mod.Macro:
    return macro_mod.Macro(
        name=name,
        steps=tuple(macro_mod.Step(code=c, at_ms=g, hold_ms=h) for c, g, h in steps),
        bind=bind,
    )


class TestTheObviousRefusals:
    def test_a_macro_with_nothing_in_it_cannot_be_played(self) -> None:
        with pytest.raises(macro_mod.MacroError, match="no keypresses"):
            macro_mod.plan(_macro([]), trigger_code=30)

    def test_a_macro_that_would_only_press_its_own_key_is_refused(self) -> None:
        """The failure this prevents is a macro that fires itself forever."""
        m = _macro([(30, 0, 40)], bind="KEY_A")
        with pytest.raises(macro_mod.MacroError, match="own key"):
            macro_mod.plan(m, trigger_code=30)

    def test_a_macro_missing_one_of_its_own_key_still_plays_the_rest(self) -> None:
        m = _macro([(30, 0, 40), (48, 10, 40)], bind="KEY_A")
        assert macro_mod.plan(m, trigger_code=30) == [
            macro_mod.Step(code=48, at_ms=10, hold_ms=40)
        ]

    def test_a_macro_containing_a_reserved_key_drops_only_that_step(self) -> None:
        m = _macro([(30, 0, 40), (48, 10, 40), (63, 10, 40)])
        steps = macro_mod.plan(m, trigger_code=None, reserved=[63])
        assert [s.code for s in steps] == [30, 48]

    def test_an_unknown_key_name_is_refused_rather_than_dropped(self) -> None:
        """Silently dropping it would play a macro that types the wrong thing."""
        entry = MacroConfig(
            name="bad",
            steps=[MacroStep(key="KEY_NOPE", at_ms=0, hold_ms=40)],
        )
        with pytest.raises(macro_mod.MacroError, match="not a key"):
            macro_mod.from_config(entry, _resolve)

    def test_a_macro_measured_in_hours_is_refused(self) -> None:
        m = _macro([(30, 0, 40)] * 20)
        # Offsets now stack, so a macro that sat idle for a minute between every
        # key really does add up to more than a minute of playback.
        slow_steps = tuple(
            macro_mod.Step(code=30, at_ms=i * 4_000, hold_ms=40) for i in range(20)
        )
        over = macro_mod.Macro(name="slow", steps=slow_steps)
        assert macro_mod.plan(m, trigger_code=None)
        with pytest.raises(macro_mod.MacroError, match="seconds"):
            macro_mod.plan(over, trigger_code=None)


class TestClamping:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (-500, 0),
            (0, 0),
            (250, 250),
            (macro_mod.MAX_GAP_MS, macro_mod.MAX_GAP_MS),
            (999_999, macro_mod.MAX_GAP_MS),
        ],
    )
    def test_a_gap_is_clamped_to_something_waitable(self, given: int, expected: int) -> None:
        assert macro_mod.clamp_gap(given) == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, 1),
            (-5, 1),
            (40, 40),
            (macro_mod.MAX_HOLD_MS, macro_mod.MAX_HOLD_MS),
            (10**6, macro_mod.MAX_HOLD_MS),
        ],
    )
    def test_a_hold_is_never_zero_because_zero_never_releases(
        self, given: int, expected: int
    ) -> None:
        assert macro_mod.clamp_hold(given) == expected

    def test_a_step_stored_an_hour_in_plays_as_the_maximum(self) -> None:
        entry = MacroConfig(
            name="lunch",
            steps=[MacroStep(key="KEY_A", at_ms=3_600_000, hold_ms=0)],
        )
        built = macro_mod.from_config(entry, _resolve)
        assert built.steps[0].at_ms == macro_mod.MAX_AT_MS
        assert built.steps[0].hold_ms == 1

    def test_a_legacy_gap_still_lands_where_it_used_to(self) -> None:
        """A macro recorded before chords existed has to play as it did.

        gap_ms is a pause from the previous release, so the offsets it implies
        are each step's start plus the previous hold plus this gap.
        """
        entry = MacroConfig(
            name="legacy",
            steps=[
                MacroStep(key="KEY_A", gap_ms=0, hold_ms=50),
                MacroStep(key="KEY_B", gap_ms=200, hold_ms=30),
                MacroStep(key="KEY_C", gap_ms=0, hold_ms=30),
            ],
        )
        built = macro_mod.from_config(entry, _resolve)
        assert [s.at_ms for s in built.steps] == [0, 250, 280]

    def test_an_explicit_offset_wins_over_a_gap_beside_it(self) -> None:
        entry = MacroConfig(
            name="mixed",
            steps=[MacroStep(key="KEY_A", at_ms=1000, gap_ms=99, hold_ms=30)],
        )
        built = macro_mod.from_config(entry, _resolve)
        assert built.steps[0].at_ms == 1000

    def test_a_later_legacy_step_does_not_slide_behind_an_explicit_one(self) -> None:
        entry = MacroConfig(
            name="mixed",
            steps=[
                MacroStep(key="KEY_A", at_ms=5000, hold_ms=30),
                MacroStep(key="KEY_B", gap_ms=10, hold_ms=30),
            ],
        )
        built = macro_mod.from_config(entry, _resolve)
        assert [s.at_ms for s in built.steps] == [5000, 5040]


class TestTimeline:
    def test_a_plain_key_is_press_then_release(self) -> None:
        assert macro_mod.timeline([macro_mod.Step(code=30, at_ms=0, hold_ms=50)]) == (
            (0, 30, 1),
            (50, 30, 0),
        )

    def test_a_chord_keeps_the_modifier_down_across_the_letter(self) -> None:
        """The flatten-to-taps bug: without interleaving this types c then ctrl."""
        events = macro_mod.timeline(
            [
                macro_mod.Step(code=29, at_ms=0, hold_ms=120),
                macro_mod.Step(code=46, at_ms=40, hold_ms=20),
            ]
        )
        assert events == (
            (0, 29, 1),
            (40, 46, 1),
            (60, 46, 0),
            (120, 29, 0),
        )

    def test_a_release_at_the_same_moment_goes_first(self) -> None:
        """Otherwise one key is briefly reported as down twice."""
        events = macro_mod.timeline(
            [
                macro_mod.Step(code=30, at_ms=0, hold_ms=100),
                macro_mod.Step(code=48, at_ms=100, hold_ms=40),
            ]
        )
        assert [value for _, _, value in events[:3]] == [1, 0, 1]

    def test_two_keys_going_down_together_keep_the_recorded_order(self) -> None:
        events = macro_mod.timeline(
            [
                macro_mod.Step(code=29, at_ms=0, hold_ms=80),
                macro_mod.Step(code=46, at_ms=0, hold_ms=80),
            ]
        )
        assert [code for _, code, value in events if value == 1] == [29, 46]

    def test_events_come_out_in_time_order_whatever_order_steps_arrive_in(self) -> None:
        events = macro_mod.timeline(
            [
                macro_mod.Step(code=48, at_ms=500, hold_ms=20),
                macro_mod.Step(code=30, at_ms=0, hold_ms=20),
            ]
        )
        assert [at for at, _, _ in events] == sorted(at for at, _, _ in events)

    def test_an_empty_macro_produces_no_events(self) -> None:
        assert macro_mod.timeline([]) == ()


class TestRecording:
    def test_a_press_and_release_becomes_one_step(self) -> None:
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        rec.feed(1.05, 30, 0)
        assert rec.finish() == (macro_mod.Step(code=30, at_ms=0, hold_ms=50),)

    def test_the_second_key_starts_after_the_first_released(self) -> None:
        """The offset comes off the previous release, not the previous start."""
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        rec.feed(1.05, 30, 0)
        rec.feed(1.25, 48, 1)
        rec.feed(1.30, 48, 0)
        assert [s.at_ms for s in rec.finish()] == [0, 250]

    def test_two_keys_held_at_once_are_both_recorded(self) -> None:
        """The whole point of chords: neither press may overwrite the other."""
        rec = macro_mod.Recorder()
        rec.feed(1.00, 29, 1)  # ctrl down
        rec.feed(1.04, 46, 1)  # c down
        rec.feed(1.06, 46, 0)  # c up
        rec.feed(1.12, 29, 0)  # ctrl up
        assert rec.finish() == (
            macro_mod.Step(code=29, at_ms=0, hold_ms=120),
            macro_mod.Step(code=46, at_ms=40, hold_ms=20),
        )

    def test_a_chord_comes_back_in_the_order_it_was_pressed(self) -> None:
        """Steps returned out of order would replay the chord backwards."""
        rec = macro_mod.Recorder()
        rec.feed(1.00, 29, 1)
        rec.feed(1.02, 30, 1)  # released first, pressed second
        rec.feed(1.05, 29, 0)
        rec.feed(1.09, 30, 0)
        assert [s.code for s in rec.finish()] == [29, 30]

    def test_a_key_never_released_is_dropped_not_guessed_at(self) -> None:
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        rec.feed(1.05, 30, 0)
        rec.feed(1.20, 48, 1)  # still down when recording stops
        assert rec.finish() == (macro_mod.Step(code=30, at_ms=0, hold_ms=50),)

    def test_an_hour_of_idle_between_presses_does_not_replay_as_an_hour(self) -> None:
        """A recording left alone over lunch would otherwise replay as a wait."""
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        rec.feed(1.05, 30, 0)
        rec.feed(3_601.05, 48, 1)
        rec.feed(3_601.10, 48, 0)
        assert rec.finish()[1].at_ms == 50 + macro_mod.MAX_GAP_MS

    def test_float_noise_does_not_eat_a_millisecond_of_every_offset(self) -> None:
        """1.25 - 1.05 is 0.19999999999999998, not 0.2.

        Truncating turned that into 199ms and did it to every offset, so a
        recorded pause was always slightly shorter than the one played.
        """
        rec = macro_mod.Recorder()
        rec.feed(1.05, 30, 1)
        rec.feed(1.10, 30, 0)
        rec.feed(1.25, 48, 1)
        rec.feed(1.30, 48, 0)
        assert [s.at_ms for s in rec.finish()] == [0, 200]

    def test_holding_a_key_does_not_record_it_dozens_of_times(self) -> None:
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        for i in range(20):
            rec.feed(1.01 + i * 0.03, 30, 2)
        rec.feed(1.80, 30, 0)
        assert len(rec.finish()) == 1

    def test_the_key_that_stops_the_recording_is_not_recorded(self) -> None:
        rec = macro_mod.Recorder(ignore=frozenset({63}))
        rec.feed(1.00, 63, 1)
        rec.feed(1.10, 63, 0)
        rec.feed(1.20, 30, 1)
        rec.feed(1.25, 30, 0)
        assert [s.code for s in rec.finish()] == [30]

    def test_a_key_still_held_when_recording_stops_is_dropped(self) -> None:
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 1)
        rec.feed(1.05, 30, 0)
        rec.feed(1.10, 48, 1)  # never released
        assert [s.code for s in rec.finish()] == [30]

    def test_a_release_with_no_press_is_ignored(self) -> None:
        rec = macro_mod.Recorder()
        rec.feed(1.00, 30, 0)
        assert rec.finish() == ()

    def test_recording_nothing_gives_an_empty_macro_rather_than_a_broken_one(self) -> None:
        assert macro_mod.Recorder().finish() == ()


class TestLayering:
    def test_a_profile_replaces_a_macro_by_name_instead_of_adding_a_second(self) -> None:
        """Two macros on one key would be a coin toss at replay time."""
        globals_ = [MacroConfig(name="hello", bind="KEY_A", steps=[MacroStep(key="KEY_B")])]
        profiles = [
            ProfileConfig(
                name="game",
                macros=[MacroConfig(name="hello", bind="KEY_B", steps=[MacroStep(key="KEY_A")])],
            )
        ]
        got = macro_mod.effective(globals_, profiles, _resolve)
        assert [m.name for m in got] == ["hello"]
        assert got[0].bind == "KEY_B"

    def test_a_profile_can_add_a_macro_that_did_not_exist_globally(self) -> None:
        profiles = [ProfileConfig(name="game", macros=[MacroConfig(name="x", bind="KEY_A")])]
        assert [m.name for m in macro_mod.effective([], profiles, _resolve)] == ["x"]

    def test_only_the_first_enabled_profile_applies(self) -> None:
        """Same rule as the remap table. Two profiles matching would be a guess."""
        profiles = [
            ProfileConfig(name="off", enabled=False, macros=[MacroConfig(name="x", bind="KEY_A")]),
            ProfileConfig(name="on", macros=[MacroConfig(name="y", bind="KEY_B")]),
        ]
        assert [m.name for m in macro_mod.effective([], profiles, _resolve)] == ["y"]

    def test_a_disabled_macro_drops_out_entirely(self) -> None:
        globals_ = [MacroConfig(name="x", bind="KEY_A", enabled=False)]
        assert macro_mod.effective(globals_, [], _resolve) == []

    def test_a_macro_with_no_bind_is_kept_but_inert(self) -> None:
        globals_ = [MacroConfig(name="x", steps=[MacroStep(key="KEY_B")])]
        got = macro_mod.effective(globals_, [], _resolve)
        assert len(got) == 1
        assert macro_mod.bindings(got, _resolve) == {}


class TestBindings:
    def test_a_key_with_two_macros_keeps_both_so_the_caller_can_complain(self) -> None:
        globals_ = [
            MacroConfig(name="a", bind="KEY_A", steps=[MacroStep(key="KEY_B")]),
            MacroConfig(name="b", bind="KEY_A", steps=[MacroStep(key="KEY_B")]),
        ]
        got = macro_mod.bindings(macro_mod.effective(globals_, [], _resolve), _resolve)
        assert [m.name for m in got[30]] == ["a", "b"]

    def test_an_unbound_macro_is_reachable_by_name_only(self) -> None:
        globals_ = [MacroConfig(name="a", steps=[MacroStep(key="KEY_B")])]
        assert macro_mod.bindings(macro_mod.effective(globals_, [], _resolve), _resolve) == {}
