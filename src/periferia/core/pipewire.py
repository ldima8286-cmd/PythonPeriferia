"""Thin wrapper over pactl and pw-dump.

Every PipeWire call in the project goes through here, so the rest of the code
never shells out on its own and every call is easy to fake in tests.

The list commands are parsed as JSON. pactl's human readable output is
translated, so parsing its labels breaks on a non English locale.

pactl answers questions about sources and modules in PulseAudio's vocabulary.
`dump` reads the graph itself instead: nodes, ports, links and the pulse
module id each node was created by, which is what `stale_modules` needs and
what a module list never carries.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Sequence
from typing import Any

from . import graph as graph_mod

log = logging.getLogger(__name__)

PACTL = "pactl"
PW_CLI = "pw-cli"
PW_DUMP = "pw-dump"

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


def have_graph() -> bool:
    """Whether pw-dump is installed at all."""
    return have(PW_DUMP)


def dump(timeout: float = 5.0) -> graph_mod.Graph:
    """Read the whole graph straight from PipeWire.

    pw-dump occasionally prints arrays with no key, which is not JSON; the
    parser names them rather than failing the whole read.
    """
    if not have_graph():
        raise PipeWireError(f"{PW_DUMP} not found")
    proc = run([PW_DUMP], timeout=timeout)
    try:
        return graph_mod.parse(proc.stdout)
    except graph_mod.GraphError as exc:
        raise PipeWireError(f"{PW_DUMP} -> {exc}") from exc


def sources() -> list[dict[str, Any]]:
    data = _json([PACTL, "-f", "json", "list", "sources"])
    return data if isinstance(data, list) else []


def modules() -> list[dict[str, Any]]:
    data = _json([PACTL, "-f", "json", "list", "short", "modules"])
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


def get_volume(name: str) -> float | None:
    """What fraction of full volume a source is carrying right now.

    Read from the same JSON the rest of this module uses, rather than from
    `get-source-volume`, because the answer needed to tell whether the gate is
    moving has to be the one the gate itself moves. pactl reports the value as
    a hex string, which is why it is not simply read as a number.
    """
    item = get_source(name)
    if item is None:
        return None
    raw = item.get("volume")
    if raw is None:
        return None
    try:
        value = int(str(raw), 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return None
    # A muted source is silent whatever its volume says, and the gate is a
    # volume rather than a mute, so this is the number worth reporting.
    return max(0.0, min(1.0, value / PA_PERCENT))


def server_ready() -> bool:
    """Whether the sound server will answer at all.

    Only distinguishes "up" from "not up". A source the server does not know
    is a different question, and one that a login-time race does not explain.
    """
    try:
        sources()
    except PipeWireError:
        return False
    return True


def wait_for_server(timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Block until the sound server answers, or the timeout runs out.

    User services and the graphical session start in parallel, so the daemon
    can win the race and find no server listening yet. Exiting there is the
    wrong answer: systemd's restart limit is five tries a minute, and a service
    that dies on the first attempt can leave the microphone dead for the rest
    of the session with nothing in the log but a refused connection.
    """
    if server_ready():
        return True
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        time.sleep(interval)
        if server_ready():
            if announced:
                log.info("the sound server is up")
            return True
        if not announced:
            log.info("waiting up to %.0fs for the sound server", timeout)
            announced = True
    return False


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


def graph_module_ids(description: str) -> list[int] | None:
    """Pulse module ids of the graph nodes carrying `description`.

    None when the graph cannot be read, which is the caller's cue to fall back
    to pactl. The id comes from the node itself, so nothing has to be matched
    back against a module list that carries no ids at all.
    """
    if not description or not have_graph():
        return None
    try:
        return dump().module_ids_for(description)
    except PipeWireError as exc:
        log.debug("graph unavailable: %s", exc)
        return None


def _stale_ids_from_sources(description: str) -> list[int]:
    """The same answer read through pactl, for when there is no graph."""
    owners: set[int] = set()
    for item in sources():
        props = item.get("properties") or {}
        named = item.get("name") == description or any(
            props.get(key) == description for key in ("device.description", "node.name")
        )
        owner = item.get("owner_module", NO_MODULE)
        if named and owner != NO_MODULE:
            owners.add(owner)
    return sorted(owners)


def stale_modules(module_name: str, description: str) -> list[int]:
    """Ids of leftover modules of ours that are still loaded.

    A killed run leaves the module up, PipeWire names the new source the same
    way, and two nodes then answer to one name. pactl resolves a name to
    whichever comes first, so the gate would drive one source while the self
    check read the other, and the level would sit frozen no matter what key was
    pressed. Matching on the description we asked for keeps this portable: the
    source name itself is not the same on every machine.

    The graph answers this directly: a node carries the description we asked
    for and the pulse module id it was created by. pactl is only consulted for
    the ids when the graph is not readable, and either way the module is
    confirmed to be ours before anything is unloaded.
    """
    if not description:
        return []
    owners = graph_module_ids(description)
    if owners is None:
        owners = _stale_ids_from_sources(description)
    if not owners:
        return []
    # "pactl list short modules" reports only name and argument, never an id,
    # so the owners found above cannot be matched against it by id. Confirm
    # the module is ours through the description we asked it to carry instead,
    # then unload the owners we already trust.
    ours = any(
        item.get("name") == module_name and description in (item.get("argument") or "")
        for item in modules()
    )
    return sorted(set(owners)) if ours else []


def unload_stale(module_name: str, description: str) -> list[int]:
    """Unload leftovers of ours, returning the ids that were removed."""
    removed: list[int] = []
    for module_id in stale_modules(module_name, description):
        if unload_module(module_id):
            removed.append(module_id)
    return removed


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
