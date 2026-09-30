"""Tests for the compositor-facing end of the bus service.

These need dbus_next, which is a dependency of the project but not always
importable where the tests run, so they skip rather than fail. What they check
is the contract the compositor script depends on: the interface name, the
object path, and the method signature. dbus-next derives the D-Bus signature
from the annotation, so a method with no return annotation is what makes this a
void call, and getting that wrong fails at import time rather than at runtime.
"""

from __future__ import annotations

import pytest

from periferia.core import windowbus

dbus_next = pytest.importorskip("dbus_next", reason="dbus-next is a runtime dependency")


class TestCoordinates:
    def test_name(self) -> None:
        assert windowbus.BUS_NAME == "org.periferia.WindowSource"

    def test_object_path(self) -> None:
        assert windowbus.OBJECT_PATH == "/org/periferia/WindowSource"

    def test_interface_matches_the_name(self) -> None:
        assert windowbus.INTERFACE == windowbus.BUS_NAME


class TestExportedMethod:
    @pytest.fixture()
    def interface(self) -> object:
        import queue

        return windowbus._source(queue.Queue())

    def test_takes_a_single_string(self, interface: object) -> None:
        assert interface.introspect().methods[0].in_signature == "s"

    def test_is_a_void_call(self, interface: object) -> None:
        assert interface.introspect().methods[0].out_signature == ""

    def test_is_named_report(self, interface: object) -> None:
        assert interface.introspect().methods[0].name == "Report"


class TestReportDelivery:
    @pytest.fixture()
    def sink(self) -> object:
        import queue

        return queue.Queue()

    @pytest.fixture()
    def call(self, sink: object) -> object:
        interface = windowbus._source(sink)
        handler = interface.Report

        def invoke(payload: str) -> None:
            handler(None, payload)

        return invoke

    def test_a_good_report_reaches_the_queue(
        self, sink: object, call: object
    ) -> None:
        call('{"resourceClass": "steam", "resourceName": "doom"}')
        assert sink.get_nowait() == {
            "resourceClass": "steam",
            "resourceName": "doom",
        }

    def test_malformed_json_becomes_an_error(self, sink: object, call: object) -> None:
        call("{not json")
        assert "error" in sink.get_nowait()

    def test_a_list_becomes_an_error(self, sink: object, call: object) -> None:
        call("[1, 2, 3]")
        assert sink.get_nowait() == {
            "error": "the compositor sent something that is not an object"
        }

    def test_empty_payload_becomes_an_error(self, sink: object, call: object) -> None:
        call("")
        assert "error" in sink.get_nowait()


class TestWithoutDependency:
    def test_the_missing_message_names_the_package(self) -> None:
        assert "dbus-next" in windowbus.MISSING
        assert "pip install" in windowbus.MISSING or "uv pip" in windowbus.MISSING
