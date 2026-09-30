from __future__ import annotations

import json

import pytest

from src.periferia.core import state as state_mod


@pytest.fixture(autouse=True)
def _runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    # A state file that survives into the real runtime directory would be
    # indistinguishable from a real one on the next run.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def test_it_records_and_reads_back() -> None:
    state_mod.write(state_mod.OPEN, source="echo-cancel-source")
    note = state_mod.read()
    assert note is not None
    assert note["state"] == state_mod.OPEN
    assert note["source"] == "echo-cancel-source"
    assert note["pid"] > 0


def test_nothing_recorded_reads_as_none() -> None:
    assert state_mod.read() is None


def test_a_corrupt_file_reads_as_none_instead_of_raising() -> None:
    # A half written or hand edited file must not turn `periferia status` into
    # a traceback.
    path = state_mod.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert state_mod.read() is None

    path.write_text("[1, 2, 3]")
    assert state_mod.read() is None


def test_json_that_is_not_an_object_reads_as_none() -> None:
    path = state_mod.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["open"]))
    assert state_mod.read() is None


def test_a_newer_note_replaces_the_old_one_completely() -> None:
    state_mod.write(state_mod.OPEN, source="a")
    state_mod.write(state_mod.CLOSED, source="b")
    note = state_mod.read()
    assert note is not None
    assert note["state"] == state_mod.CLOSED
    assert note["source"] == "b"
    # no .json.new left behind to be mistaken for the real file
    leftovers = [p for p in state_mod.state_path().parent.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_writing_never_raises_when_the_directory_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The microphone being open matters more than the note about it.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/proc/nonexistent-nope")
    state_mod.write(state_mod.OPEN, source="x")
    assert state_mod.read() is None


def test_clear_removes_the_note() -> None:
    state_mod.write(state_mod.OPEN, source="a")
    state_mod.clear()
    assert state_mod.read() is None
    state_mod.clear()  # twice must not raise
