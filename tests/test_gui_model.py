"""Tests for the window's logic. No Qt, no display."""

from __future__ import annotations

from typing import ClassVar

import pytest
from evdev import ecodes

from src.periferia.core.config import PointerConfig, ProfileConfig
from src.periferia.gui import model
from src.periferia.modules.remap import RemapError


def code(name: str) -> int:
    return ecodes.ecodes[name]


class TestDrafts:
    def test_empty_profiles_give_no_drafts(self):
        assert model.drafts_from_config([]) == []

    def test_a_profile_becomes_a_draft(self):
        entries = [ProfileConfig(name="game", remap={"KEY_CAPSLOCK": "KEY_ESC"})]
        assert model.first_editable(entries).rows == [("KEY_CAPSLOCK", "KEY_ESC")]

    def test_every_profile_is_editable_now(self):
        """Each one used to be unreachable, which meant a profile that names a
        window could be written but not changed."""
        entries = [
            ProfileConfig(name="a", remap={"KEY_A": "KEY_B"}),
            ProfileConfig(name="b", remap={"KEY_C": "KEY_D"}),
        ]
        assert [d.name for d in model.drafts_from_config(entries)] == ["a", "b"]

    def test_a_match_field_the_window_does_not_know_is_left_out(self):
        """A profile matched on something this window cannot ask for keeps it in
        the file; dropping it here would delete it on the next save."""
        entries = [ProfileConfig(name="x", match={"resource_class": "steam", "title": "x"})]
        assert model.first_editable(entries).match == {"resource_class": "steam"}

    def test_the_pointer_speed_comes_along(self):
        entries = [
            ProfileConfig(name="g", pointer=PointerConfig(speed=2.5)),
        ]
        assert model.first_editable(entries).speed == 2.5

    def test_a_profile_with_no_speed_says_none_rather_than_the_default(self):
        """A default of 1.0 written back would put a key in the file that the
        user never asked for."""
        entries = [ProfileConfig(name="g")]
        assert model.first_editable(entries).speed is None

    def test_the_window_opens_on_the_one_that_does_something(self):
        entries = [
            ProfileConfig(name="fallback"),
            ProfileConfig(name="game", remap={"KEY_A": "KEY_B"}),
        ]
        assert model.first_editable(entries).name == "game"

    def test_only_empty_profiles_still_open_on_the_first(self):
        entries = [ProfileConfig(name="a"), ProfileConfig(name="b")]
        assert model.first_editable(entries).name == "a"

    def test_a_blank_field_stops_existing(self):
        """An empty criterion would be written and then could never match, which
        looks exactly like a broken match."""
        draft = model.ProfileDraft(name="x", match={"resource_class": "  "})
        assert draft.clean().match == {}


class TestEditing:
    def test_add_appends(self):
        assert model.add_row([], "KEY_A", "KEY_B") == [("KEY_A", "KEY_B")]

    def test_add_refuses_a_modifier_target(self):
        with pytest.raises(RemapError, match="stuck down"):
            model.add_row([], "KEY_A", "KEY_LEFTSHIFT")

    def test_add_refuses_a_bad_table_already_present(self):
        with pytest.raises(RemapError):
            model.add_row([("KEY_A", "KEY_LEFTSHIFT")], "KEY_B", "KEY_C")

    def test_remove_takes_the_right_row(self):
        rows = [("KEY_A", "KEY_B"), ("KEY_C", "KEY_D")]
        assert model.remove_row(rows, 1) == [("KEY_A", "KEY_B")]

    def test_remove_out_of_range(self):
        with pytest.raises(IndexError):
            model.remove_row([("KEY_A", "KEY_B")], 5)

    def test_duplicate_source_is_caught_on_save(self):
        with pytest.raises(RemapError, match="twice"):
            model.rows_to_mapping([("KEY_A", "KEY_B"), ("KEY_A", "KEY_C")])

    def test_validity_check_reports_the_reason(self):
        ok, reason = model.rows_are_valid([("KEY_A", "KEY_B")])
        assert ok and reason == ""
        ok, reason = model.rows_are_valid([("KEY_A", "KEY_LEFTCTRL")])
        assert not ok and "stuck down" in reason


class TestPttWarning:
    def test_quiet_when_ptt_is_not_remapped(self):
        assert model.check_rows([("KEY_A", "KEY_B")], code("KEY_GRAVE")) is None

    def test_warns_when_ptt_is_remapped(self):
        message = model.check_rows([("KEY_GRAVE", "KEY_F13")], code("KEY_GRAVE"))
        assert message is not None
        assert "GRAVE" in message

    def test_warns_when_panic_is_remapped(self):
        message = model.check_rows([("KEY_F12", "KEY_A")], code("KEY_F12"))
        assert message is not None


class TestSaving:
    def test_writes_the_profile(self, tmp_path):
        path = tmp_path / "config.yaml"
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_CAPSLOCK", "KEY_ESC")]))
        assert "KEY_CAPSLOCK" in path.read_text()

    def test_round_trip_returns_the_same_rows(self, tmp_path):
        from src.periferia.core.config import load

        path = tmp_path / "config.yaml"
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_CAPSLOCK", "KEY_ESC")]))
        assert model.first_editable(load(path).profiles).rows == [("KEY_CAPSLOCK", "KEY_ESC")]

    def test_empty_table_removes_the_remap_but_keeps_the_profile(self, tmp_path):
        """A profile that stops remapping keys is still a profile: it may still
        say which window it belongs to."""
        from src.periferia.core.config import load

        path = tmp_path / "config.yaml"
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_B")]))
        model.save_draft(path, model.ProfileDraft(name="game", match={"resource_class": "steam"}))

        left = load(path).profiles
        assert len(left) == 1
        assert left[0].name == "game"
        assert left[0].remap == {}

    @pytest.mark.skipif(
        not model.HAVE_ROUND_TRIP, reason="needs ruamel.yaml for comment preservation"
    )
    def test_comments_survive_a_save(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "# моя настройка\nptt:\n  ptt_key: KEY_GRAVE  # е\nprofiles:\n"
            "  - name: game\n    remap:\n      KEY_A: KEY_B\n",
            encoding="utf-8",
        )
        model.save_draft(
            path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_B"), ("KEY_C", "KEY_D")])
        )
        text = path.read_text()
        assert "# моя настройка" in text
        assert "# е" in text
        assert "KEY_C" in text

    def test_other_sections_are_left_alone(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  hold_ms: 150\nptt:\n  ptt_key: KEY_GRAVE\n", encoding="utf-8")
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_B")]))
        from src.periferia.core.config import load

        cfg = load(path)
        assert cfg.audio.hold_ms == 150
        assert cfg.ptt.ptt_key == "KEY_GRAVE"

    def test_unknown_sections_survive(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ptt:\n  ptt_key: KEY_GRAVE\nfuture_thing:\n  x: 1\n", encoding="utf-8")
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_B")]))
        assert "future_thing" in path.read_text()


class TestKeys:
    def test_picker_offers_the_russian_letter(self):
        by_name = {k.name: k for k in model.available_keys()}
        assert by_name["KEY_GRAVE"].label == "Ё (GRAVE)"
        assert by_name["KEY_A"].label == "Ф (A)"

    def test_label_carries_the_keycode_name_too(self):
        by_name = {k.name: k for k in model.available_keys()}
        assert "GRAVE" in by_name["KEY_GRAVE"].label

    def test_name_is_what_the_config_gets(self):
        for choice in model.available_keys():
            assert choice.name.startswith("KEY_")

    def test_picker_has_no_buttons(self):
        assert all(k.name.startswith("KEY_") for k in model.available_keys())

    def test_picker_is_sorted_by_name_not_label(self):
        names = [k.name for k in model.available_keys()]
        assert names == sorted(names)

    def test_order_is_stable_between_calls(self):
        assert [k.name for k in model.available_keys()] == [
            k.name for k in model.available_keys()
        ]

    def test_non_letter_keys_have_no_cyrillic(self):
        by_name = {k.name: k for k in model.available_keys()}
        assert by_name["KEY_1"].label == "1"
        assert by_name["KEY_ESC"].label == "ESC"

    def test_resolve_label_matches_the_tui(self):
        from evdev import ecodes

        from src.periferia.modules.hotkey import key_label as tui_label

        assert model.resolve_label("KEY_GRAVE") == tui_label(ecodes.ecodes["KEY_GRAVE"])

    def test_unknown_name_passes_through(self):
        assert model.resolve_label("KEY_NOPE") == "KEY_NOPE"


class TestStatus:
    def test_missing_state_file_means_the_daemon_is_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        status = model.read_status()
        assert status.state == "unknown"
        assert status.text == "Демон не запущен"

    def test_reads_the_state_the_daemon_wrote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        from src.periferia.core import state as state_mod

        # The real constant, not a hand-typed spelling of it. This test used to
        # write "LATCHED" and check against "LATCHED", which agreed with itself
        # and with nothing the daemon produces.
        state_mod.write(state_mod.LATCHED, source="PeriferiaMic")
        status = model.read_status()
        assert status.state == state_mod.LATCHED
        assert status.latched
        assert not status.open

    @pytest.mark.parametrize(
        ("constant", "opened", "text"),
        [
            ("OPEN", True, "Микрофон открыт"),
            ("CLOSED", False, "Микрофон закрыт"),
            ("LATCHED", False, "Микрофон залип"),
            ("CLOSING", True, "Микрофон закрывается"),
            ("PANIC", False, "Паника"),
        ],
    )
    def test_wording_per_state(self, tmp_path, monkeypatch, constant, opened, text):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        from src.periferia.core import state as state_mod

        state_mod.write(getattr(state_mod, constant), source="PeriferiaMic")
        status = model.read_status()
        assert status.open is opened
        assert status.text == text

    def test_describe_mentions_the_keys(self):
        detail = model.MicStatus(state="OPEN").describe("GRAVE", "F12")
        assert "GRAVE" in detail and "F12" in detail

    def test_age_is_reported_when_known(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        from src.periferia.core import state as state_mod

        state_mod.write(state_mod.OPEN, source="PeriferiaMic")
        assert model.read_status().age is not None


def _keys() -> list[model.KeyChoice]:
    return [
        model.KeyChoice(code=i, name=f"KEY_{name}", label=name)
        for i, name in enumerate("ABCDEF", start=1)
    ]


class TestSuggest:
    KEYS: ClassVar[list[model.KeyChoice]] = _keys()

    def test_picks_two_free_keys(self):
        assert model.suggest_row([], self.KEYS) == ("KEY_A", "KEY_B")

    def test_avoids_keys_already_in_use(self):
        rows = [("KEY_A", "KEY_B")]
        assert model.suggest_row(rows, self.KEYS) == ("KEY_C", "KEY_D")

    def test_avoids_keys_used_as_targets(self):
        rows = [("KEY_D", "KEY_A")]
        assert model.suggest_row(rows, self.KEYS) == ("KEY_B", "KEY_C")

    def test_suggestion_is_always_a_valid_table(self):
        rows = [("KEY_A", "KEY_B"), ("KEY_C", "KEY_D")]
        suggestion = model.suggest_row(rows, self.KEYS)
        assert suggestion is not None
        ok, _ = model.rows_are_valid([*rows, suggestion])
        assert ok

    def test_none_when_too_few_keys_left(self):
        rows = [("KEY_A", "KEY_B"), ("KEY_C", "KEY_D"), ("KEY_E", "KEY_F")]
        assert model.suggest_row(rows, self.KEYS) is None

    def test_none_when_only_one_key_is_free(self):
        rows = [("KEY_A", "KEY_B"), ("KEY_C", "KEY_D"), ("KEY_E", "KEY_A")]
        assert model.suggest_row(rows, self.KEYS) is None

    def test_none_on_an_empty_key_list(self):
        assert model.suggest_row([], []) is None


@pytest.mark.skipif(not model.HAVE_ROUND_TRIP, reason="needs ruamel.yaml")
class TestFormatting:
    def test_indented_lists_keep_their_indent(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n  - name: game\n    remap:\n      KEY_A: KEY_B\n", encoding="utf-8"
        )
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_C")]))
        assert "  - name: game" in path.read_text()

    def test_flush_lists_stay_flush(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n- name: game\n  remap:\n    KEY_A: KEY_B\n", encoding="utf-8"
        )
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_C")]))
        assert "\n- name: game" in path.read_text()

    def test_quoted_values_keep_their_quotes(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text('ptt:\n  ptt_key: "KEY_GRAVE"\n', encoding="utf-8")
        model.save_draft(path, model.ProfileDraft(name="game", rows=[("KEY_A", "KEY_B")]))
        assert '"KEY_GRAVE"' in path.read_text()


class TestStatusAgainstRealStateConstants:
    """The daemon writes lowercase constants, so the window has to read those.

    The window used to compare against its own uppercase spellings, which
    matched nothing the daemon ever wrote. Every state fell through to
    "not running", so a live microphone reported as a dead daemon.
    """

    def test_every_daemon_state_is_recognised(self):
        from src.periferia.core import state

        names = {
            state.OPEN: "Микрофон открыт",
            state.CLOSING: "Микрофон закрывается",
            state.LATCHED: "Микрофон залип",
            state.CLOSED: "Микрофон закрыт",
            state.PANIC: "Паника",
        }
        for written, expected in names.items():
            got = model.MicStatus(state=written, running=True).text
            assert got == expected, f"{written!r} gave {got!r}"

    def test_state_strings_are_the_ones_the_daemon_writes(self):
        from src.periferia.core import state

        for written in (state.OPEN, state.LATCHED, state.PANIC):
            assert written == written.lower()

    def test_open_follows_the_constants(self):
        from src.periferia.core import state

        assert model.MicStatus(state=state.OPEN).open
        assert model.MicStatus(state=state.CLOSING).open
        assert not model.MicStatus(state=state.CLOSED).open

    def test_latched_follows_the_constants(self):
        from src.periferia.core import state

        assert model.MicStatus(state=state.LATCHED).latched
        assert not model.MicStatus(state=state.CLOSED).latched
