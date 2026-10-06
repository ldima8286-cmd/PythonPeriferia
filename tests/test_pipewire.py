from __future__ import annotations

import argparse
import importlib

import pytest


class TestGetVolume:
    """pactl reports a source's volume as a hex string, and the gate is only
    proved to work if the number read back is the one that was written."""

    def test_it_reads_pacts_hex_string(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "get_source", lambda n: {"volume": "0x00010000"})
        assert pw.get_volume("x") == 1.0

    def test_zero_is_zero(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "get_source", lambda n: {"volume": "0x00000000"})
        assert pw.get_volume("x") == 0.0

    def test_half_is_half(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "get_source", lambda n: {"volume": "0x00008000"})
        assert pw.get_volume("x") == pytest.approx(0.5)

    def test_a_missing_source_is_none_not_zero(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "get_source", lambda n: None)
        assert pw.get_volume("x") is None

    def test_nonsense_reads_as_none(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        for bad in ({"volume": "wat"}, {"volume": None}, {}):
            monkeypatch.setattr(pw, "get_source", lambda n, b=bad: b)
            assert pw.get_volume("x") is None

    def test_it_is_clamped_to_the_range_pactl_reports(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "get_source", lambda n: {"volume": "0x00020000"})
        assert pw.get_volume("x") == 1.0


class TestGraphReadStraightFromPwDump:
    """Cleanup used to chase module ids through a list that has no id in it.
    The graph carries the id on the node itself, so that is read first and
    pactl is only a fallback."""

    def test_the_ids_come_off_the_graph(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        graph = importlib.import_module("periferia.core.graph")

        fake = graph.Graph(
            nodes=(
                graph.Node(
                    id=71,
                    name="echo-cancel-source",
                    description="PeriferiaMic",
                    media_class="Audio/Source",
                    pulse_module_id=536870916,
                ),
            )
        )
        monkeypatch.setattr(pw, "dump", lambda timeout=5.0: fake)
        monkeypatch.setattr(pw, "have_graph", lambda: True)

        assert pw.graph_module_ids("PeriferiaMic") == [536870916]

    def test_no_description_means_no_lookup(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")

        def boom(timeout: float = 5.0):
            raise AssertionError("an empty description must not reach pw-dump")

        monkeypatch.setattr(pw, "dump", boom)
        monkeypatch.setattr(pw, "have_graph", lambda: True)
        assert pw.graph_module_ids("") is None

    def test_an_unreadable_graph_falls_back_rather_than_raising(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")

        def raise_error(timeout: float = 5.0):
            raise pw.PipeWireError("can't connect")

        monkeypatch.setattr(pw, "dump", raise_error)
        monkeypatch.setattr(pw, "have_graph", lambda: True)
        assert pw.graph_module_ids("PeriferiaMic") is None

    def test_no_pw_dump_at_all_is_a_fallback_not_a_failure(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "have_graph", lambda: False)
        assert pw.graph_module_ids("PeriferiaMic") is None

    def test_dump_reports_a_broken_read_as_a_pipewire_error(self, monkeypatch):
        pw = importlib.import_module("periferia.core.pipewire")
        monkeypatch.setattr(pw, "have_graph", lambda: True)

        class _Proc:
            stdout = "not json"

        monkeypatch.setattr(pw, "run", lambda args, timeout=5.0: _Proc())
        with pytest.raises(pw.PipeWireError, match="did not parse"):
            pw.dump()


class TestGraphCheck:
    def test_an_unreadable_graph_is_a_warning_not_a_failure(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: False)
        result = cli_mod.envcheck.check_graph()
        assert result.status == cli_mod.envcheck.WARN
        assert "falls back" in result.hint

    def test_a_readable_graph_is_ok(self, monkeypatch):
        from src.periferia import cli as cli_mod

        graph = importlib.import_module("periferia.core.graph")
        monkeypatch.setattr(
            cli_mod.pipewire, "dump", lambda timeout=5.0: graph.Graph(nodes=(), links=())
        )
        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: True)
        result = cli_mod.envcheck.check_graph()
        assert result.status == cli_mod.envcheck.OK
        assert "0 nodes" in result.detail

    def test_two_nodes_answering_to_one_name_is_a_warning(self, monkeypatch):
        from src.periferia import cli as cli_mod

        graph = importlib.import_module("periferia.core.graph")
        monkeypatch.setattr(
            cli_mod.pipewire,
            "dump",
            lambda timeout=5.0: graph.Graph(
                nodes=(
                    graph.Node(
                        id=1, name="dup", description="", media_class="", pulse_module_id=None
                    ),
                    graph.Node(
                        id=2, name="dup", description="", media_class="", pulse_module_id=None
                    ),
                ),
                links=(),
            ),
        )
        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: True)
        result = cli_mod.envcheck.check_graph()
        assert result.status == cli_mod.envcheck.WARN
        assert "dup" in result.detail


class TestGraphCommand:
    def test_it_refuses_when_pw_dump_is_missing(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: False)
        assert cli_mod.cmd_graph(argparse.Namespace()) == 1
        assert "pw-dump not found" in capsys.readouterr().err

    def test_an_unreadable_graph_is_reported_once(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: True)

        def raise_error(timeout: float = 5.0):
            raise cli_mod.pipewire.PipeWireError("can't connect")

        monkeypatch.setattr(cli_mod.pipewire, "dump", raise_error)
        assert cli_mod.cmd_graph(argparse.Namespace()) == 1
        assert "can't connect" in capsys.readouterr().err

    def test_it_prints_the_nodes_it_found(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        graph = importlib.import_module("periferia.core.graph")
        monkeypatch.setattr(cli_mod.pipewire, "have_graph", lambda: True)
        monkeypatch.setattr(
            cli_mod.pipewire,
            "dump",
            lambda timeout=5.0: graph.Graph(
                nodes=(
                    graph.Node(
                        id=71,
                        name="echo-cancel-source",
                        description="PeriferiaMic",
                        media_class="Audio/Source",
                        pulse_module_id=536870916,
                    ),
                ),
                links=(),
            ),
        )
        assert cli_mod.cmd_graph(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert "echo-cancel-source" in out
        assert "PeriferiaMic" in out
        assert "536870916" in out
        assert "none" in out, "an empty link list has to say so"


class TestGateCheckNeedsNothingButASource:
    def test_it_refuses_when_there_is_nothing_to_watch(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.state_mod, "read", lambda: None)
        args = argparse.Namespace(source=None, timeout=1.0)
        assert cli_mod.cmd_gate_check(args) == 1
        assert "no source to watch" in capsys.readouterr().err

    def test_a_missing_source_is_reported_as_missing(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.state_mod, "read", lambda: {"source": "gone", "pid": 1})
        monkeypatch.setattr(cli_mod.pipewire, "get_volume", lambda n: None)
        args = argparse.Namespace(source=None, timeout=1.0)
        assert cli_mod.cmd_gate_check(args) == 1
        assert "does not exist" in capsys.readouterr().err

    def test_a_gate_that_moves_passes(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.state_mod, "read", lambda: {"source": "s", "pid": 1})
        monkeypatch.setattr(cli_mod.state_mod, "is_running", lambda n: True)
        levels = iter([0.0, 0.9, 1.0])
        monkeypatch.setattr(cli_mod.pipewire, "get_volume", lambda n: next(levels, 1.0))
        args = argparse.Namespace(source=None, timeout=5.0)
        assert cli_mod.cmd_gate_check(args) == 0
        assert "the gate moved" in capsys.readouterr().out

    def test_a_gate_that_never_moves_fails(self, monkeypatch, capsys):
        from src.periferia import cli as cli_mod

        monkeypatch.setattr(cli_mod.state_mod, "read", lambda: {"source": "s", "pid": 1})
        monkeypatch.setattr(cli_mod.state_mod, "is_running", lambda n: True)
        monkeypatch.setattr(cli_mod.pipewire, "get_volume", lambda n: 0.0)
        args = argparse.Namespace(source=None, timeout=0.0)
        assert cli_mod.cmd_gate_check(args) == 1
        assert "did not move" in capsys.readouterr().out
