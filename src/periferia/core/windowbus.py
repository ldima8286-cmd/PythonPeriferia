"""Window reports from the compositor, over the session bus.

Per-window profiles need to know which window is focused, and the compositor is
the only thing that knows. On Plasma the compositor will tell us, if we give it
a place to say so, and this is that place: a name on the session bus that a
compositor script calls when the active window changes.

The compositor's own log is deliberately not used. A release build of Plasma
ships with debug categories switched off, so a script's print() goes nowhere,
and the silence is indistinguishable from a script that threw before printing.
The bus is the one channel guaranteed to be on.

The service runs on its own thread with its own event loop, because the rest of
the project is synchronous and a GLib main loop in the middle of the daemon
would own the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    s = str

BUS_NAME = "org.periferia.WindowSource"
OBJECT_PATH = "/org/periferia/WindowSource"
INTERFACE = "org.periferia.WindowSource"

try:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method

    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False
    MessageBus = Any  # type: ignore[assignment,misc]


MISSING = (
    "dbus-next is not installed, so the compositor has nowhere to report to\n"
    "install it into the project's own environment:\n"
    "  uv pip install dbus-next   (or: python -m pip install dbus-next)"
)


def _source(sink: queue.Queue[dict[str, Any]]) -> Any:
    class WindowSource(ServiceInterface):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__(INTERFACE)

        @method()
        def Report(self, payload: s):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                sink.put({"error": "the compositor sent something that is not JSON"})
                return
            if isinstance(data, dict):
                sink.put(data)
            else:
                sink.put({"error": "the compositor sent something that is not an object"})

    return WindowSource()


class WindowService:
    """A background listener for compositor reports."""

    def __init__(self) -> None:
        self.reports: queue.Queue[dict[str, Any]] = queue.Queue()
        self.error = ""
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(timeout=8.0)
        return self.error == ""

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()
            with contextlib.suppress(Exception):
                self._loop.close()

    async def _run(self) -> None:
        bus = await MessageBus().connect()
        bus.export(OBJECT_PATH, _source(self.reports))
        await bus.request_name(BUS_NAME)
        self._ready.set()
        await bus.wait_for_disconnect()

    def next_report(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.reports.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._thread.is_alive():
            with contextlib.suppress(Exception):
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)
