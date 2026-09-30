"""Receives window reports from the compositor over the session bus.

Run as its own process, under an interpreter that has PyGObject:

    /usr/bin/python3 src/periferia/core/_window_receiver.py

It is deliberately standalone: no imports from the package, so the system
interpreter can run it even when the project's virtualenv cannot see
PyGObject. Results go to stdout, one JSON object per line, so the caller can
read them without a bus library of its own.

The compositor's own log is not used. A release build of Plasma ships with
debug categories switched off, so a script's print() goes nowhere at all, and
the failure is indistinguishable from a script that threw before it printed.
The bus is the one channel guaranteed to be on.
"""

import contextlib
import json
import os
import sys
from typing import Any

BUS_NAME = "org.periferia.WindowSource"
OBJECT_PATH = "/org/periferia/WindowSource"
INTERFACE = "org.periferia.WindowSource"


def emit(**fields: Any) -> None:
    print(json.dumps(fields), flush=True)


def make_report_handler() -> Any:
    def on_report(
        connection: Any,
        sender: Any,
        path: Any,
        interface: Any,
        method: Any,
        params: Any,
        invocation: Any,
    ) -> None:
        try:
            payload = params.unpack()[0]
            report = json.loads(payload)
            emit(report=report if isinstance(report, dict) else {"error": "not an object"})
        except Exception as error:
            emit(report={"error": f"bad payload: {type(error).__name__}: {error}"})
        finally:
            with contextlib.suppress(Exception):
                invocation.return_value(None)

    return on_report


def main() -> int:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as error:
        emit(ready=False, error=f"PyGObject is unusable: {type(error).__name__}: {error}")
        return 1

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.register_object(OBJECT_PATH, make_report_handler(), None, None)
        Gio.bus_own_name_on_connection(
            connection, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, None, None
        )
    except Exception as error:
        emit(ready=False, error=f"could not take {BUS_NAME}: {type(error).__name__}: {error}")
        return 1

    loop = GLib.MainLoop()
    emit(ready=True, name=BUS_NAME, pid=os.getpid())
    with contextlib.suppress(KeyboardInterrupt):
        loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
