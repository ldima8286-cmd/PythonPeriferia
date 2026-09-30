from __future__ import annotations

import json
import os
import time

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


class TestIsRunning:
    """A note outlives the process that wrote it, and readers must notice."""

    def test_absent_note_is_not_running(self):
        assert not state_mod.is_running(None)

    def test_own_pid_is_running(self):
        assert state_mod.is_running({"pid": os.getpid(), "state": "open"})

    def test_note_from_a_dead_process_is_not_running(self):
        dead = _dead_pid()
        assert not state_mod.is_running({"pid": dead, "state": "panic"})

    def test_missing_pid_is_not_running(self):
        assert not state_mod.is_running({"state": "open"})
        assert not state_mod.is_running({"pid": None, "state": "open"})

    def test_nonsense_pid_is_not_running(self):
        for bad in (0, -1, "abc", 1.5, True):
            assert not state_mod.is_running({"pid": bad, "state": "open"})

    def test_a_stale_open_note_does_not_look_live(self):
        dead = _dead_pid()
        note = {"pid": dead, "state": "open", "changed_at": time.time()}
        assert not state_mod.is_running(note)

    def test_our_own_pid_still_reads_the_state_it_just_wrote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        state_mod.write(state_mod.OPEN, source="echo-cancel-source")
        note = state_mod.read()
        assert note is not None
        assert state_mod.is_running(note)


def _dead_pid() -> int:
    for candidate in range(40000, 40050):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise AssertionError("no free pid found")


class TestAge:
    def test_absent_note_has_no_age(self):
        assert state_mod.age(None) is None

    def test_age_is_measured_from_the_note(self):
        assert state_mod.age({"changed_at": time.time() - 30}) == pytest.approx(30, abs=2)

    def test_clock_skew_does_not_produce_a_negative_age(self):
        assert state_mod.age({"changed_at": time.time() + 500}) == 0.0

    def test_missing_or_nonsense_timestamp_has_no_age(self):
        assert state_mod.age({}) is None
        assert state_mod.age({"changed_at": "soon"}) is None


class TestWriteRefusesUnknownStates:
    def test_every_constant_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        for value in state_mod.ALL:
            state_mod.write(value, source="t")
            assert state_mod.read()["state"] == value

    def test_a_misspelled_state_is_not_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        state_mod.write("LATCHED", source="t")
        assert state_mod.read() is None

    def test_recording_a_bad_state_leaves_the_good_note_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        state_mod.write(state_mod.OPEN, source="t")
        state_mod.write("PANIC!", source="t")
        assert state_mod.read()["state"] == state_mod.OPEN

    def test_all_holds_exactly_the_named_states(self):
        assert {"closed", "open", "closing", "latched", "panic"} == state_mod.ALL
