"""The pointer speed, with KWin standing in as a recording.

Every call here is a subprocess in real use, and the whole point of the module
is what happens when that subprocess is missing or says no.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.periferia.core import kwinconfig


def _done(code: int, out: str = "") -> Any:
    return type("Done", (), {"returncode": code, "stdout": out, "stderr": ""})()


class FakeRun:
    """Stands in for kwinconfig._run: records argv, answers from a table."""

    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[bool, str]:
        self.calls.append(argv)
        tool = argv[0].rsplit("/", 1)[-1]
        code, out = self.answers.get(tool, (0, ""))
        return code == 0, out


@pytest.fixture
def tools(monkeypatch):
    """Both tools present, and a reconfigure that answers."""
    monkeypatch.setattr(kwinconfig.shutil, "which", lambda name: f"/usr/bin/{name}")
    return FakeRun({})


def test_nothing_is_written_when_the_tools_are_missing(monkeypatch) -> None:
    """A desktop without kde-config-tools should still push to talk."""
    monkeypatch.setattr(kwinconfig.shutil, "which", lambda name: None)
    assert kwinconfig.available() is False
    assert kwinconfig.write_speed(0.5) is False
    assert kwinconfig.read_speed() is None


def test_the_newest_tool_is_preferred(monkeypatch) -> None:
    names = {"kreadconfig6": "/usr/bin/kreadconfig6", "kreadconfig5": "/usr/bin/kreadconfig5"}
    monkeypatch.setattr(kwinconfig.shutil, "which", lambda name: names.get(name))
    assert kwinconfig.read_tool() == "/usr/bin/kreadconfig6"


def test_the_older_tool_is_used_when_that_is_all_there_is(monkeypatch) -> None:
    names = {"kreadconfig5": "/usr/bin/kreadconfig5"}
    monkeypatch.setattr(kwinconfig.shutil, "which", lambda name: names.get(name))
    assert kwinconfig.read_tool() == "/usr/bin/kreadconfig5"
    assert kwinconfig.write_tool() is None
    assert kwinconfig.available() is False


def test_writing_asks_kwin_to_reread_the_file(tools, monkeypatch) -> None:
    """Without the reconfigure the value sits in kwinrc doing nothing until
    the next login, which looks exactly like the write having failed."""
    monkeypatch.setattr(kwinconfig, "_run", tools)

    assert kwinconfig.write_speed(0.5) is True

    assert tools.calls[0][-3:] == ["Mouse", "speed", "0.5"]
    assert tools.calls[1][-3:] == ["org.kde.KWin", "/KWin", "reconfigure"]


def test_a_value_that_was_not_written_is_not_written_back(tools, monkeypatch) -> None:
    """KWin's default is not the same as a key in the file the user never
    wrote, and adding one would make the change invisible in their settings."""
    monkeypatch.setattr(kwinconfig, "_run", tools)

    assert kwinconfig.read_speed() is None


def test_the_speed_is_read_from_the_file(tools, monkeypatch) -> None:
    monkeypatch.setattr(kwinconfig, "_run", FakeRun({"kreadconfig6": (0, "0.35\n")}))

    assert kwinconfig.read_speed() == pytest.approx(0.35)


def test_a_speed_that_is_not_a_number_is_not_guessed_at(monkeypatch) -> None:
    monkeypatch.setattr(kwinconfig.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kwinconfig, "_run", FakeRun({"kreadconfig6": (0, "true")}))

    assert kwinconfig.read_speed() is None


def test_a_failed_write_is_not_reported_as_a_change(tools, monkeypatch) -> None:
    monkeypatch.setattr(kwinconfig, "_run", FakeRun({"kwriteconfig6": (1, "")}))

    assert kwinconfig.write_speed(0.5) is False


def test_a_file_written_but_not_reread_still_says_so(tools, monkeypatch) -> None:
    """The value is in the file and will work at the next login. Saying
    nothing would look like the profile was ignored."""
    monkeypatch.setattr(
        kwinconfig,
        "_run",
        FakeRun({"qdbus6": (1, "no such object"), "qdbus": (1, "no such object")}),
    )

    assert kwinconfig.write_speed(0.5) is False


def test_either_dbus_interface_name_is_enough(tools, monkeypatch) -> None:
    answers: dict[str, tuple[int, str]] = {}
    real = FakeRun(answers)

    def only_the_old_one(argv: list[str]) -> tuple[bool, str]:
        if argv[0].endswith("qdbus6"):
            return False, "no such interface"
        return real(argv)

    monkeypatch.setattr(kwinconfig, "_run", only_the_old_one)

    assert kwinconfig.reconfigure() is True


def test_a_profile_without_a_speed_puts_the_original_back(tools, monkeypatch) -> None:
    """A config that speeds the pointer up in one window must not leave it fast
    in every other window."""
    monkeypatch.setattr(kwinconfig, "_run", tools)

    assert kwinconfig.apply(None, restore=0.4) is True
    assert tools.calls[0][-1] == "0.4"


def test_a_profile_without_a_speed_and_nothing_to_restore_does_nothing(
    tools, monkeypatch
) -> None:
    monkeypatch.setattr(kwinconfig, "_run", tools)

    assert kwinconfig.apply(None, restore=None) is False
    assert tools.calls == []
