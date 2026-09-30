"""Tests for the bus receiver.

The receiver cannot be run against a real bus in a test, and PyGObject is not
importable in the environment the tests run in. What is testable is the logic
written here: that a report turns into one line of JSON, that a malformed one
is survived rather than crashing the service, and that a missing PyGObject is
reported instead of raising. The GIO calls themselves stay unverified, and
that is stated rather than papered over.
"""

from __future__ import annotations

import builtins
import json
import sys

import pytest

from periferia.core import _window_receiver as receiver


class FakeParams:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def unpack(self) -> tuple[str]:
        return (self._payload,)


class FakeInvocation:
    def __init__(self) -> None:
        self.returned = False

    def return_value(self, value: object) -> None:
        self.returned = True


def _run_handler(payload: str, capsys: pytest.CaptureFixture[str]) -> FakeInvocation:
    invocation = FakeInvocation()
    receiver.make_report_handler()(
        None, None, None, None, "Report", FakeParams(payload), invocation
    )
    return invocation


class TestReportHandler:
    def test_emits_the_report_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        invocation = _run_handler('{"resourceClass": "steam"}', capsys)
        line = json.loads(capsys.readouterr().out.strip())
        assert line == {"report": {"resourceClass": "steam"}}
        assert invocation.returned is True

    def test_survives_malformed_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        invocation = _run_handler("{not json", capsys)
        line = json.loads(capsys.readouterr().out.strip())
        assert "error" in line["report"]
        assert invocation.returned is True

    def test_rejects_a_non_object(self, capsys: pytest.CaptureFixture[str]) -> None:
        _run_handler("[1, 2, 3]", capsys)
        line = json.loads(capsys.readouterr().out.strip())
        assert line == {"report": {"error": "not an object"}}

    def test_answers_even_when_the_payload_is_bad(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class Exploding:
            def unpack(self) -> None:
                raise ValueError("gone")

        invocation = FakeInvocation()
        receiver.make_report_handler()(
            None, None, None, None, "Report", Exploding(), invocation
        )
        assert invocation.returned is True
        assert "error" in json.loads(capsys.readouterr().out.strip())["report"]


class TestEmit:
    def test_one_line_per_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        receiver.emit(ready=True)
        receiver.emit(ready=False, error="nope")
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"ready": True}
        assert json.loads(lines[1])["error"] == "nope"


class TestWithoutPyGObject:
    def test_reports_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        real_import = builtins.__import__

        def blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "gi" or name.startswith("gi."):
                raise ImportError("no gi here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        monkeypatch.delitem(sys.modules, "gi", raising=False)
        assert receiver.main() == 1
        line = json.loads(capsys.readouterr().out.strip())
        assert line["ready"] is False
        assert "PyGObject" in line["error"]


class TestCoordinates:
    def test_name_and_path_are_absolute(self) -> None:
        assert receiver.BUS_NAME.startswith("org.periferia.")
        assert receiver.OBJECT_PATH.startswith("/")

    def test_the_script_calls_the_same_coordinates(self) -> None:
        from periferia.core import activewindow

        rendered = activewindow.SCRIPT % {
            "marker": activewindow.MARKER,
            "bus": receiver.BUS_NAME,
            "path": receiver.OBJECT_PATH,
            "iface": receiver.INTERFACE,
        }
        assert receiver.BUS_NAME in rendered
        assert receiver.OBJECT_PATH in rendered
        assert "Report" in rendered
