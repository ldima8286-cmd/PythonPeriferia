"""The graph is read straight from pw-dump, so it is parsed off the wire here.

The dumps in tests/fixtures are real ones: pw-dump emits nameless arrays
inside a params map, which is not valid json and made an earlier whole-file
repair path give up. That shape is what pwdump_broken.json carries.
"""

import json
from pathlib import Path

import pytest

from periferia.core import graph as graph_mod
from periferia.core.graph import GraphError

FIXTURES = Path(__file__).parent / "fixtures"
SUBSET = FIXTURES / "pwdump_subset.json"
BROKEN = FIXTURES / "pwdump_broken.json"


def test_the_subset_fixture_is_a_clean_dump() -> None:
    text = SUBSET.read_text(encoding="utf-8")
    json.loads(text)
    assert graph_mod.parse(text).nodes


def test_repair_names_the_nameless_arrays_pw_dump_emits() -> None:
    text = BROKEN.read_text(encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)

    fixed, added = graph_mod.repair(text)

    assert added == 2, "the fixture carries one real defect, naming it takes two keys"
    json.loads(fixed)


def test_repair_leaves_a_clean_dump_alone() -> None:
    text = SUBSET.read_text(encoding="utf-8")
    fixed, added = graph_mod.repair(text)

    assert added == 0
    assert fixed == text


def test_repair_is_not_fooled_by_brackets_inside_strings() -> None:
    text = '[ {"a": "value [ with } brackets and \\" quotes", "b": [1, 2]} ]'
    fixed, added = graph_mod.repair(text)

    assert added == 0
    assert json.loads(fixed) == json.loads(text)


def test_parse_reads_nodes_ports_and_links_from_the_real_dump() -> None:
    parsed = graph_mod.parse(BROKEN.read_text(encoding="utf-8"))

    source = parsed.nodes_named("echo-cancel-source")[0]
    assert source.id == 71
    assert source.description == "PeriferiaMic"
    assert source.media_class == "Audio/Source"
    assert source.pulse_module_id == 536870916

    assert {n.name for n in parsed.nodes} == {
        "echo-cancel-capture",
        "echo-cancel-source",
        "echo-cancel-sink",
        "echo-cancel-playback",
    }

    ports = parsed.ports_of(71)
    assert {p.name for p in ports} == {"capture_FL", "capture_FR"}
    assert all(p.direction == "output" for p in ports)

    links = parsed.links_of(70)
    assert {link.id for link in links} == {83, 84}
    assert all(link.input_node == 70 for link in links)
    assert {link.output_node for link in links} == {55}
    assert all(link.state for link in links)
    assert parsed.links_of(71) == [], "the source node is not linked yet"


def test_nodes_are_found_by_description_not_only_by_name() -> None:
    parsed = graph_mod.parse(BROKEN.read_text(encoding="utf-8"))

    ours = parsed.nodes_described_as("PeriferiaMic")
    assert [n.name for n in ours] == ["echo-cancel-source"]
    assert parsed.nodes_described_as("nobody-elses-device") == []


def test_module_ids_come_off_the_node_itself() -> None:
    parsed = graph_mod.parse(BROKEN.read_text(encoding="utf-8"))

    assert parsed.module_ids_for("PeriferiaMic") == [536870916]
    assert parsed.module_ids_for("nobody") == []


def test_duplicate_descriptions_are_reported_once_per_extra_copy() -> None:
    objects = json.loads(SUBSET.read_text(encoding="utf-8"))
    source = next(o for o in objects if o.get("id") == 71)
    twin = json.loads(json.dumps(source))
    twin["id"] = 171

    parsed = graph_mod.build([*objects, twin])

    assert parsed.duplicates() == {"echo-cancel-source": [71, 171]}
    # Both copies come from the same module, so the id is reported once.
    assert parsed.module_ids_for("PeriferiaMic") == [536870916]
    assert graph_mod.build([objects[0]]).duplicates() == {}


def test_garbage_is_a_graph_error_not_a_traceback() -> None:
    with pytest.raises(GraphError, match="did not parse"):
        graph_mod.parse("not json at all")


def test_an_object_without_an_id_is_skipped_rather_than_fatal() -> None:
    parsed = graph_mod.build([{"type": "PipeWire:Interface:Node", "info": {}}, {"id": 7}])

    assert parsed.nodes == ()
    assert parsed.node(7) is None
