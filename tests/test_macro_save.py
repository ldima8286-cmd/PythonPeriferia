"""Writing a macro into the config.

The risk in saving is not the macro, it is everything else in the file. This
config carries comments explaining why the audio path is the way it is, and a
function that rewrote the document from its own idea of the schema would delete
those and every section it has not heard of. So the tests here check what
survived, not just what was written.
"""

from __future__ import annotations

from pathlib import Path

from periferia.core.config import load
from periferia.gui import model


def _config(extra: str = "") -> str:
    return (
        "audio:\n"
        "  attack_ms: 10  # ramp, so the mic does not click\n"
        "ptt:\n"
        "  ptt_key: KEY_GRAVE\n"
        f"{extra}"
    )


class TestSavingAGlobalMacro:
    def test_the_macro_lands_in_the_config_and_loads_back(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_H", 120, 40), ("KEY_I", 60, 40)], bind="KEY_F5")

        cfg = load(path)
        assert len(cfg.macros) == 1
        assert cfg.macros[0].name == "hello"
        assert cfg.macros[0].bind == "KEY_F5"
        assert [s.key for s in cfg.macros[0].steps] == ["KEY_H", "KEY_I"]
        assert [s.gap_ms for s in cfg.macros[0].steps] == [120, 60]

    def test_the_timings_survive_exactly(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "m", [("KEY_A", 7, 3)], bind="")
        assert load(path).macros[0].steps[0].gap_ms == 7
        assert load(path).macros[0].steps[0].hold_ms == 3

    def test_recording_over_a_macro_of_the_same_name_replaces_it(self, tmp_path: Path) -> None:
        """Two macros with one name is ambiguous about which one a key plays."""
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        model.save_macro(path, "hello", [("KEY_B", 0, 40), ("KEY_C", 0, 40)], bind="KEY_F6")

        cfg = load(path)
        assert len(cfg.macros) == 1
        assert cfg.macros[0].bind == "KEY_F6"
        assert [s.key for s in cfg.macros[0].steps] == ["KEY_B", "KEY_C"]

    def test_other_macros_are_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "one", [("KEY_A", 0, 40)], bind="KEY_F5")
        model.save_macro(path, "two", [("KEY_B", 0, 40)], bind="KEY_F6")
        assert {m.name for m in load(path).macros} == {"one", "two"}


class TestSavingWithoutLosingAnything:
    def test_comments_survive(self, tmp_path: Path) -> None:
        """The audio section explains why it is shaped that way. Losing that
        costs the next person the reasoning, which is the whole point of it."""
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        assert "ramp, so the mic does not click" in path.read_text(encoding="utf-8")

    def test_the_rest_of_the_config_is_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        cfg = load(path)
        assert cfg.ptt.ptt_key == "KEY_GRAVE"
        assert cfg.audio.attack_ms == 10

    def test_saving_twice_does_not_duplicate_the_section(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config(), encoding="utf-8")
        for _ in range(3):
            model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        assert path.read_text(encoding="utf-8").count("name: hello") == 1

    def test_an_absent_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "config.yaml"
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        assert load(path).macros[0].name == "hello"


class TestPerProfileCopies:
    def test_a_copy_lands_in_the_named_profile_and_nowhere_else(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            _config(
                "profiles:\n"
                "  - name: game\n"
                "    match:\n"
                "      resource_class: steam\n"
                "  - name: rest\n",
            ),
            encoding="utf-8",
        )
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5", profiles=["game"])
        cfg = load(path)
        assert [m.name for m in cfg.profiles[0].macros] == ["hello"]
        assert cfg.profiles[1].macros == []
        assert cfg.macros == []

    def test_the_profile_copy_can_be_bound_differently(self, tmp_path: Path) -> None:
        """The reason for having per-profile macros at all.

        Two calls, because that is how it is done: place the recording globally
        on one key, then place it again in the profile on another. The second
        call leaves the global copy alone, which is what makes one recording
        reachable on both keys.
        """
        path = tmp_path / "config.yaml"
        path.write_text(
            _config("profiles:\n  - name: game\n    match:\n      resource_class: steam\n"),
            encoding="utf-8",
        )
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="KEY_F5")
        model.save_macro(
            path, "hello", [("KEY_A", 0, 40)], bind="KEY_F6", profiles=["game"]
        )
        cfg = load(path)
        assert cfg.macros[0].bind == "KEY_F5"
        assert cfg.profiles[0].macros[0].bind == "KEY_F6"

    def test_a_profile_that_does_not_exist_is_reported_not_fatal(self, tmp_path: Path) -> None:
        """A typo in --in-profile should not silently write nothing anywhere."""
        path = tmp_path / "config.yaml"
        path.write_text(_config("profiles:\n  - name: game\n"), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="", profiles=["nosuch"])
        cfg = load(path)
        assert cfg.profiles[0].macros == []
        assert [m.name for m in cfg.macros] == ["hello"]

    def test_the_profiles_section_is_not_removed_when_it_had_no_match(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_config("profiles:\n  - name: game\n"), encoding="utf-8")
        model.save_macro(path, "hello", [("KEY_A", 0, 40)], bind="", profiles=["game"])
        assert [p.name for p in load(path).profiles] == ["game"]
