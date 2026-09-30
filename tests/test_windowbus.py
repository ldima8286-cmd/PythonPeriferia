"""Tests for the compositor-facing end of the bus service.

The contract the compositor script depends on is small: an interface name, an
object path, a method called Report, and a single string argument. Those are
checked by introspection, which needs no bus at all.

Delivery is checked over a real session bus, because the alternative is calling
the decorated method directly, which does not work: the decorator wraps the
function and passes an extra argument. Dispatch is the thing worth testing and
it is the thing that cannot be faked.
"""

from __future__ import annotations

import asyncio
import queue
from typing import Any

import pytest

from periferia.core import windowbus

pytest.importorskip("dbus_next", reason="dbus-next is a runtime dependency")


@pytest.fixture()
def sink() -> queue.Queue[dict[str, Any]]:
    return queue.Queue()


class TestCoordinates:
    def test_name(self) -> None:
        assert windowbus.BUS_NAME == "org.periferia.WindowSource"

    def test_object_path(self) -> None:
        assert windowbus.OBJECT_PATH == "/org/periferia/WindowSource"

    def test_interface_matches_the_name(self) -> None:
        assert windowbus.INTERFACE == windowbus.BUS_NAME


class TestSignature:
    def _method(self, sink: queue.Queue[dict[str, Any]]) -> Any:
        return windowbus._source(sink).introspect().methods[0]

    def test_takes_a_single_string(self, sink: queue.Queue[dict[str, Any]]) -> None:
        assert self._method(sink).in_signature == "s"

    def test_is_a_void_call(self, sink: queue.Queue[dict[str, Any]]) -> None:
        assert self._method(sink).out_signature == ""

    def test_is_named_report(self, sink: queue.Queue[dict[str, Any]]) -> None:
        assert self._method(sink).name == "Report"


def _roundtrip(payload: str) -> tuple[str, dict[str, Any]]:
    from dbus_next import Message
    from dbus_next.aio import MessageBus

    async def go() -> tuple[str, dict[str, Any]]:
        sink: queue.Queue[dict[str, Any]] = queue.Queue()
        bus = await MessageBus().connect()
        bus.export(windowbus.OBJECT_PATH, windowbus._source(sink))
        unique = getattr(bus, "unique_name", None) or bus._name
        reply = await bus.call(
            Message(
                destination=unique,
                path=windowbus.OBJECT_PATH,
                interface=windowbus.INTERFACE,
                member="Report",
                signature="s",
                body=[payload],
            )
        )
        return reply.message_type.name, sink.get_nowait()

    return asyncio.run(go())


def _has_bus() -> bool:
    from dbus_next.aio import MessageBus

    async def check() -> bool:
        try:
            await MessageBus().connect()
        except Exception:
            return False
        return True

    try:
        return asyncio.run(check())
    except Exception:
        return False


needs_bus = pytest.mark.skipif(not _has_bus(), reason="no session bus here")


@needs_bus
class TestOverRealBus:
    def test_a_good_report_reaches_the_queue(self) -> None:
        kind, received = _roundtrip('{"resourceClass": "steam", "caption": "DOOM"}')
        assert kind == "METHOD_RETURN"
        assert received == {"resourceClass": "steam", "caption": "DOOM"}

    def test_malformed_json_is_survived(self) -> None:
        kind, received = _roundtrip("{not json")
        assert kind == "METHOD_RETURN"
        assert "error" in received

    def test_a_list_is_refused(self) -> None:
        _, received = _roundtrip("[1, 2, 3]")
        assert received == {
            "error": "the compositor sent something that is not an object"
        }

    def test_the_script_payload_shape_is_accepted(self) -> None:
        _, received = _roundtrip(
            '{"fields": {"caption": {"kind": "string", "text": "Steam"},'
            ' "resourceClass": {"kind": "string", "text": "steam"}},'
            ' "sequence": 1}'
        )
        assert received["fields"]["resourceClass"]["text"] == "steam"


class TestWithoutDependency:
    def test_the_missing_message_names_the_package(self) -> None:
        assert "dbus-next" in windowbus.MISSING
        assert "pip install" in windowbus.MISSING
