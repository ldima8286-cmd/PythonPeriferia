"""The command line end to end: parse, run, and read the output back.

These call the parser and the command functions directly, never a shell. That
keeps them usable on a machine with no input devices and no PipeWire, which
is almost everywhere the tests can run.
"""

from __future__ import annotations

import json

import pytest
import yaml

from periferia.cli import build_parser

FULL = """\
ptt:
  ptt_key: KEY_F10
audio:
  physical_source: auto
profiles:
  - name: game
    match:
      resource_class: steam
    remap:
      KEY_CAPSLOCK: KEY_TAB
  - name: default
    remap:
      KEY_CAPSLOCK: KEY_ESC
devices:
  - name: headset
    match:
      device.bus: usb
    audio:
      target_volume: 0.8
macros:
  - name: combo
    bind: KEY_F5
    steps:
      - key: KEY_LEFTCTRL
        at_ms: 0
        hold_ms: 40
"""


def run(*argv: str) -> int:
    args = build_parser().parse_args(list(argv))
    return args.func(args)


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_help_lists_the_commands():
    help_text = build_parser().format_help()
    for name in (
        "profiles",
        "export",
        "import",
        "config",
        "macro",
        "status",
        "check",
        "init-config",
    ):
        assert name in help_text


def test_the_parser_refuses_to_run_without_a_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_config_json(tmp_path, capsys):
    path = _write(tmp_path, FULL)
    assert run("-c", str(path), "config", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["profiles"][0]["name"] == "game"
    assert data["devices"][0]["name"] == "headset"


def test_config_check_passes_a_healthy_config(tmp_path, capsys):
    path = _write(tmp_path, FULL)
    assert run("-c", str(path), "config", "--check") == 0
    assert "every check passed" in capsys.readouterr().out


def test_config_check_names_the_broken_value(tmp_path, capsys):
    path = _write(tmp_path, "audio:\n  attack_ms: 'fast'\n")
    assert run("-c", str(path), "config", "--check") == 1
    captured = capsys.readouterr()
    assert "config is invalid" in captured.err
    assert "attack_ms" in captured.err


def test_profiles_list_names_every_profile(tmp_path, capsys):
    path = _write(tmp_path, FULL)
    assert run("-c", str(path), "profiles") == 0
    out = capsys.readouterr().out
    assert "game" in out
    assert "default" in out
    assert "resource_class=steam" in out
    assert "fallback" in out


def test_profiles_export_then_import(tmp_path, capsys):
    source = _write(tmp_path, FULL)
    exported = tmp_path / "profiles.yaml"
    assert run("-c", str(source), "profiles", "export", str(exported)) == 0
    block = yaml.safe_load(exported.read_text(encoding="utf-8"))
    assert set(block) == {"profiles"}
    assert block["profiles"][0]["name"] == "game"

    empty = _write(tmp_path, "ptt:\n  ptt_key: KEY_F10\n")
    assert run("-c", str(empty), "profiles", "import", str(exported)) == 0
    assert "imported" in capsys.readouterr().out
    assert run("-c", str(empty), "profiles") == 0
    assert "default" in capsys.readouterr().out


def test_macro_delete_rewrites_the_config(tmp_path, capsys):
    path = _write(tmp_path, FULL)
    assert run("-c", str(path), "macro", "delete", "combo") == 0
    assert "deleted" in capsys.readouterr().out
    assert "combo" not in path.read_text(encoding="utf-8")


def test_init_config_writes_the_starter(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "periferia.core.config.default_config_path", lambda: target
    )
    assert run("init-config") == 0
    text = target.read_text(encoding="utf-8")
    assert "profiles:" in text
    assert "ptt:" in text
    assert "wrote" in capsys.readouterr().out


def test_init_config_keeps_an_existing_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config.yaml"
    target.write_text("ptt:\n  ptt_key: KEY_F9\n", encoding="utf-8")
    monkeypatch.setattr(
        "periferia.core.config.default_config_path", lambda: target
    )
    assert run("init-config") == 1
    assert "already exists" in capsys.readouterr().err
    assert "KEY_F9" in target.read_text(encoding="utf-8")