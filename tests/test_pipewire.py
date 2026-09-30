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
