"""The graph as PipeWire itself lists it, read through pw-dump.

pactl speaks PulseAudio's vocabulary: sources, owner modules, a module list
that never carries an id. The graph is the truth of what is running: every
node with its name, its description and the pulse module that created it,
every port, every link between them. This module only reads it. The one
subprocess involved lives in `pipewire.py`, as everywhere else in the project.

pw-dump is not always well formed. Its `params` objects occasionally contain
arrays with no key at all, which is not JSON and makes the whole dump
unreadable; `repair` names those so the rest of the file survives.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class GraphError(RuntimeError):
    """The dump could not be read as a graph."""


def repair(text: str) -> tuple[str, int]:
    """Give the keyless arrays in `text` a name of their own.

    Returns the fixed text and how many names were added. Scanning is done by
    hand rather than by a regex because the decision depends on context: a
    `[` right after a `,` inside an object is a missing key, the same `[`
    after a `:` is an ordinary value.
    """
    out: list[str] = []
    # Containers open so far, as the character that opened them.
    stack: list[str] = []
    # True when the next token of the current object is its key.
    expect_key = False
    in_string = False
    escaped = False
    added = 0

    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            out.append(char)
            if expect_key:
                # A key has been read; the colon comes next.
                expect_key = False
        elif char in "{[":
            if expect_key:
                out.append(f'"_unnamed_{added}": ')
                added += 1
                expect_key = False
            out.append(char)
            stack.append(char)
            expect_key = char == "{"
        elif char in "}]":
            if stack:
                stack.pop()
            expect_key = False
            out.append(char)
        elif char == ",":
            out.append(char)
            expect_key = bool(stack) and stack[-1] == "{"
        elif char == ":":
            out.append(char)
            expect_key = False
        else:
            out.append(char)
            if not char.isspace() and expect_key:
                # A number or a literal where a key belongs is malformed in a
                # way this function does not try to fix; stop waiting for one.
                expect_key = False

    return "".join(out), added


def parse(text: str) -> Graph:
    """Turn the text pw-dump printed into a graph."""
    fixed, added = repair(text)
    try:
        data = json.loads(fixed)
    except json.JSONDecodeError as exc:
        detail = " (after naming keyless arrays)" if added else ""
        raise GraphError(f"pw-dump did not parse as json{detail}: {exc}") from exc
    if not isinstance(data, list):
        raise GraphError("pw-dump did not print a list of objects")
    return build(data)


def build(objects: Sequence[Mapping[str, Any]]) -> Graph:
    """Pick nodes, ports, links and modules out of the dumped objects."""
    nodes: list[Node] = []
    ports: list[Port] = []
    links: list[Link] = []
    modules: list[Module] = []

    for item in objects:
        kind = item.get("type")
        info = item.get("info")
        if not isinstance(info, Mapping):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, int):
            continue

        if kind == "PipeWire:Interface:Node":
            props = _props(info)
            pulse_id = props.get("pulse.module.id")
            nodes.append(
                Node(
                    id=raw_id,
                    name=str(props.get("node.name") or ""),
                    description=str(
                        props.get("node.description") or props.get("device.description") or ""
                    ),
                    media_class=str(props.get("media.class") or ""),
                    pulse_module_id=pulse_id if isinstance(pulse_id, int) else None,
                    props=props,
                )
            )
        elif kind == "PipeWire:Interface:Port":
            props = _props(info)
            node_id = props.get("node.id")
            ports.append(
                Port(
                    id=raw_id,
                    node_id=node_id if isinstance(node_id, int) else -1,
                    direction=str(info.get("direction") or props.get("port.direction") or ""),
                    name=str(props.get("port.name") or props.get("object.path") or ""),
                )
            )
        elif kind == "PipeWire:Interface:Link":
            links.append(
                Link(
                    id=raw_id,
                    output_node=_int(info.get("output-node-id"), -1),
                    output_port=_int(info.get("output-port-id"), -1),
                    input_node=_int(info.get("input-node-id"), -1),
                    input_port=_int(info.get("input-port-id"), -1),
                    state=str(info.get("state") or ""),
                )
            )
        elif kind == "PipeWire:Interface:Module":
            modules.append(
                Module(
                    id=raw_id,
                    name=str(info.get("name") or ""),
                    args=str(info.get("args") or ""),
                )
            )

    return Graph(
        nodes=tuple(nodes),
        ports=tuple(ports),
        links=tuple(links),
        modules=tuple(modules),
    )


def _props(info: Mapping[str, Any]) -> dict[str, Any]:
    props = info.get("props")
    return dict(props) if isinstance(props, Mapping) else {}


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) else default


@dataclass(frozen=True)
class Node:
    id: int
    name: str
    description: str
    media_class: str
    pulse_module_id: int | None
    props: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Port:
    id: int
    node_id: int
    direction: str
    name: str


@dataclass(frozen=True)
class Link:
    id: int
    output_node: int
    output_port: int
    input_node: int
    input_port: int
    state: str


@dataclass(frozen=True)
class Module:
    id: int
    name: str
    args: str


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...] = ()
    ports: tuple[Port, ...] = ()
    links: tuple[Link, ...] = ()
    modules: tuple[Module, ...] = ()

    def node(self, node_id: int) -> Node | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def nodes_named(self, name: str) -> list[Node]:
        """Every node answering to `name`.

        More than one means a module was loaded twice, which is the state the
        cleanup exists for: pactl resolves the name to whichever comes first.
        """
        return [node for node in self.nodes if node.name == name]

    def nodes_described_as(self, description: str) -> list[Node]:
        """Nodes carrying `description`, the handle that outlives a reload."""
        if not description:
            return []
        return [node for node in self.nodes if node.description == description]

    def module_ids_for(self, description: str) -> list[int]:
        """Pulse module ids of the nodes carrying `description`.

        The id pactl unloads by, read from the node that was created by that
        very module, so nothing has to be matched back to a module list which
        does not carry ids at all.
        """
        found = {
            node.pulse_module_id
            for node in self.nodes_described_as(description)
            if node.pulse_module_id is not None
        }
        return sorted(found)

    def ports_of(self, node_id: int) -> list[Port]:
        return [port for port in self.ports if port.node_id == node_id]

    def links_of(self, node_id: int) -> list[Link]:
        return [
            link
            for link in self.links
            if link.input_node == node_id or link.output_node == node_id
        ]

    def duplicates(self) -> dict[str, list[int]]:
        """Names answered by more than one node, with the ids that answer."""
        counts: dict[str, list[int]] = {}
        for node in self.nodes:
            if node.name:
                counts.setdefault(node.name, []).append(node.id)
        return {name: ids for name, ids in counts.items() if len(ids) > 1}
