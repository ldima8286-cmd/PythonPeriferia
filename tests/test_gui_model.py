"""Tests for the window's logic. No Qt, no display."""

from __future__ import annotations

from typing import ClassVar

import pytest
from evdev import ecodes

from src.periferia.core.config import ProfileConfig
from src.periferia.gui import model
from src.periferia.modules.remap import RemapError


def code(name: str) -> int:
    return ecodes.ecodes[name]


class TestRows:
    def test_empty_profiles_give_no_rows(self):
        assert model.rows_from_profiles([]) == []

    def test_disabled_profile_is_ignored(self):
        entries = [ProfileConfig(name="x", enabled=False, remap={"KEY_A": "KEY_B"})]
        assert model.rows_from_profiles(entries) == []

    def test_enabled_profile_becomes_rows(self):
        entries = [ProfileConfig(name="game", remap={"KEY_CAPSLOCK": "KEY_ESC"})]
        assert model.rows_from_profiles(entries) == [("KEY_CAPSLOCK", "KEY_ESC")]

    def test_only_the_first_enabled_profile_is_editable(self):
        entries = [
            ProfileConfig(name="a", remap={"KEY_A": "KEY_B"}),
            ProfileConfig(name="b", remap={"KEY_C": "KEY_D"}),
        ]
        assert model.rows_from_profiles(entries) == [("KEY_A", "KEY_B")]

    def test_name_comes_from_the_profile_in_effect(self):
        assert model.profile_name([ProfileConfig(name="game", remap={"KEY_A": "KEY_B"})]) == "game"
        assert model.profile_name([]) == "default"


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
        model.save_profiles(path, [("KEY_CAPSLOCK", "KEY_ESC")], "game")
        assert "KEY_CAPSLOCK" in path.read_text()

    def test_round_trip_returns_the_same_rows(self, tmp_path):
        from src.periferia.core.config import load

        path = tmp_path / "config.yaml"
        model.save_profiles(path, [("KEY_CAPSLOCK", "KEY_ESC")], "game")
        assert model.rows_from_profiles(load(path).profiles) == [("KEY_CAPSLOCK", "KEY_ESC")]

    def test_empty_table_removes_the_section(self, tmp_path):
        path = tmp_path / "config.yaml"
        model.save_profiles(path, [("KEY_A", "KEY_B")], "game")
        model.save_profiles(path, [], "game")
        from src.periferia.core.config import load

        assert load(path).profiles == []

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
        model.save_profiles(path, [("KEY_A", "KEY_B"), ("KEY_C", "KEY_D")], "game")
        text = path.read_text()
        assert "# моя настройка" in text
        assert "# е" in text
        assert "KEY_C" in text

    def test_other_sections_are_left_alone(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("audio:\n  hold_ms: 150\nptt:\n  ptt_key: KEY_GRAVE\n", encoding="utf-8")
        model.save_profiles(path, [("KEY_A", "KEY_B")], "game")
        from src.periferia.core.config import load

        cfg = load(path)
        assert cfg.audio.hold_ms == 150
        assert cfg.ptt.ptt_key == "KEY_GRAVE"

    def test_unknown_sections_survive(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ptt:\n  ptt_key: KEY_GRAVE\nfuture_thing:\n  x: 1\n", encoding="utf-8")
        model.save_profiles(path, [("KEY_A", "KEY_B")], "game")
        assert "future_thing" in path.read_text()


class TestKeys:
    def test_picker_offers_keys(self):
        keys = model.available_keys()
        assert any(k.label == "A" for k in keys)
        assert all(k.label != "" for k in keys)

    def test_picker_has_no_buttons(self):
        assert all(not k.label.startswith("BTN") for k in model.available_keys())

    def test_picker_order_is_stable(self):
        assert [k.label for k in model.available_keys()] == [
            k.label for k in model.available_keys()
        ]

    def test_label_strips_the_prefix(self):
        assert model.resolve_label("KEY_GRAVE") == "GRAVE"

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

        state_mod.write("LATCHED", source="PeriferiaMic")
        status = model.read_status()
        assert status.state == "LATCHED"
        assert status.latched
        assert not status.open

    @pytest.mark.parametrize(
        ("state", "opened", "text"),
        [
            ("OPEN", True, "Микрофон открыт"),
            ("CLOSED", False, "Микрофон закрыт"),
            ("LATCHED", False, "Микрофон залип"),
            ("CLOSING", True, "Микрофон закрывается"),
            ("PANIC", False, "Паника"),
        ],
    )
    def test_wording_per_state(self, tmp_path, monkeypatch, state, opened, text):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        from src.periferia.core import state as state_mod

        state_mod.write(state, source="PeriferiaMic")
        status = model.read_status()
        assert status.open is opened
        assert status.text == text

    def test_describe_mentions_the_keys(self):
        detail = model.MicStatus(state="OPEN").describe("GRAVE", "F12")
        assert "GRAVE" in detail and "F12" in detail

    def test_age_is_reported_when_known(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        from src.periferia.core import state as state_mod

        state_mod.write("OPEN", source="PeriferiaMic")
        assert model.read_status().age is not None


def _keys() -> list[model.KeyChoice]:
    return [model.KeyChoice(code=i, label=name) for i, name in enumerate("ABCDEF", start=1)]


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
        model.save_profiles(path, [("KEY_A", "KEY_C")], "game")
        assert "  - name: game" in path.read_text()

    def test_flush_lists_stay_flush(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "profiles:\n- name: game\n  remap:\n    KEY_A: KEY_B\n", encoding="utf-8"
        )
        model.save_profiles(path, [("KEY_A", "KEY_C")], "game")
        assert "\n- name: game" in path.read_text()

    def test_quoted_values_keep_their_quotes(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text('ptt:\n  ptt_key: "KEY_GRAVE"\n', encoding="utf-8")
        model.save_profiles(path, [("KEY_A", "KEY_B")], "game")
        assert '"KEY_GRAVE"' in path.read_text()
