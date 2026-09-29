"""Thin wrapper over pactl.

Every PipeWire call in the project goes through here, so the rest of the code
never shells out on its own and every call is easy to fake in tests.

The list commands are parsed as JSON. pactl's human readable output is
translated, so parsing its labels breaks on a non English locale.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

PACTL = "pactl"
PW_CLI = "pw-cli"

PA_PERCENT = 65536
NO_MODULE = 4294967295


class PipeWireError(RuntimeError):
    pass


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def run(
    args: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    log.debug("run: %s", " ".join(args))
    try:
        proc = subprocess.run(
            list(args),
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipeWireError(f"{' '.join(args)} failed: {exc}") from exc

    if check and proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        detail = err[-1] if err else f"rc={proc.returncode}"
        raise PipeWireError(f"{' '.join(args)} -> {detail}")
    return proc


def _json(args: Sequence[str]) -> Any:
    proc = run(args)
    # pactl prints locale warnings on stderr for some builds; stdout is the json.
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise PipeWireError(f"{' '.join(args)} returned invalid json: {exc}") from exc


def sources() -> list[dict[str, Any]]:
    data = _json([PACTL, "-f", "json", "list", "sources"])
    return data if isinstance(data, list) else []


def short_sources() -> list[dict[str, Any]]:
    data = _json([PACTL, "-f", "json", "list", "short", "sources"])
    return data if isinstance(data, list) else []


def source_names() -> list[str]:
    return [item["name"] for item in sources() if item.get("name")]


def get_source(name: str) -> dict[str, Any] | None:
    for item in sources():
        if item.get("name") == name:
            return item
    return None


def source_exists(name: str) -> bool:
    return get_source(name) is not None


def is_virtual(name: str) -> bool:
    """A source created by a module has an owner module and no hardware class."""
    item = get_source(name)
    if item is None:
        return True
    if item.get("owner_module", NO_MODULE) != NO_MODULE:
        return True
    props = item.get("properties") or {}
    device_class = props.get("device.class")
    return device_class not in ("sound", "monitor")


def is_candidate(name: str) -> bool:
    """Real capture hardware, not a monitor and not a module source."""
    item = get_source(name)
    if item is None:
        return False
    if item.get("owner_module", NO_MODULE) != NO_MODULE:
        return False
    props = item.get("properties") or {}
    if props.get("device.class") != "sound":
        return False
    if "monitor" in name:
        return False
    return bool(props.get("api.alsa.card") or props.get("device.api"))


def source_by_module(module_id: int) -> str | None:
    for item in sources():
        if item.get("owner_module") == module_id:
            name = item.get("name")
            if name:
                return str(name)
    return None


def find_source(description: str) -> str | None:
    """Find a source by the name we asked PipeWire to describe it with.

    module-echo-cancel names its own output after the machine, so the stable
    handle is the device.description passed in source_properties.
    """
    if not description:
        return None
    for item in sources():
        if item.get("name") == description:
            return str(description)
        props = item.get("properties") or {}
        for key in ("device.description", "node.name"):
            if props.get(key) == description:
                name = item.get("name")
                if name:
                    return str(name)
    return None


def default_source() -> str | None:
    proc = run([PACTL, "get-default-source"], check=False)
    if proc.returncode != 0:
        return None
    name = proc.stdout.strip()
    return name or None


def set_default_source(name: str) -> None:
    run([PACTL, "set-default-source", name])


def set_volume(name: str, fraction: float) -> None:
    raw = max(0, min(round(fraction * PA_PERCENT), PA_PERCENT))
    run([PACTL, "set-source-volume", name, str(raw)])


def set_mute(name: str, mute: bool) -> None:
    run([PACTL, "set-source-mute", name, "1" if mute else "0"])


def load_module(name: str, args: Sequence[str]) -> int | None:
    argv = [PACTL, "load-module", name, *args]
    proc = run(argv, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        log.warning("load-module %s failed: %s", name, err[-1] if err else "unknown error")
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        log.warning("load-module %s returned no id", name)
        return None


def unload_module(module_id: int) -> bool:
    return run([PACTL, "unload-module", str(module_id)], check=False).returncode == 0
