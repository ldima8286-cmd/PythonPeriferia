"""Receives window reports from the compositor over the session bus.

Run as its own process, under an interpreter that has PyGObject:

    /usr/bin/python3 src/periferia/core/_window_receiver.py

It is deliberately standalone: no imports from the package, so the system
interpreter can run it even when the project's virtualenv cannot see
PyGObject. Results are written to stdout, one JSON object per line, so the
caller can read it without any bus library of its own.

The compositor's own log is not used. A release build of Plasma ships with
debug categories switched off, so a script's print() goes nowhere at all, and
the failure is indistinguishable from a script that threw before it printed.
The bus is the only channel that is guaranteed to be on.
"""

import contextlib
import json
import os
import sys
from typing import Any

BUS_NAME = "org.periferia.WindowSource"
OBJECT_PATH = "/org/periferia/WindowSource"
INTERFACE = "org.periferia.WindowSource"

INTROSPECTION = """
<node>
  <interface name="org.periferia.WindowSource">
    <method name="Report">
      <arg name="payload" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""


def emit(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def main() -> int:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as error:
        emit(ready=False, error=f"PyGObject is unusable: {type(error).__name__}: {error}")
        return 1

    loop = GLib.MainLoop()

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
            emit(report=json.loads(payload))
        except Exception as error:
            emit(report={"error": f"bad payload: {error}"})
        finally:
            invocation.return_value(None)

    def on_introspect(
        connection: Any,
        sender: Any,
        path: Any,
        interface: Any,
        method: Any,
        params: Any,
        invocation: Any,
    ) -> None:
        invocation.return_value(GLib.Variant("(s)", (INTROSPECTION,)))

    try:
        address = Gio.DBusAddress("unix:path=object=")
        flags = (
            Gio.DBusNodeFlags.AUTHENTICATION_CLIENT
            | Gio.DBusNodeFlags.MESSAGE_BUS_CONNECTION
        )
        node = Gio.DBusNode.new_for_address_sync(address, flags, None, None)
        connection = node.new_connection_sync(None, None, None, None, None, None)
        connection.register_object(OBJECT_PATH, on_report, None, None)
        Gio.bus_own_name_on_connection(
            connection, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, None, None
        )
    except Exception as error:
        emit(ready=False, error=f"could not take {BUS_NAME}: {error}")
        return 1

    emit(ready=True, name=BUS_NAME, pid=os.getpid())
    with contextlib.suppress(KeyboardInterrupt):
        loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
