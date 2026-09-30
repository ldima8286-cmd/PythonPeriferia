"""The window is built with Qt's offscreen platform, so these need no display."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.periferia.core import state as state_mod
from src.periferia.gui.model import rows_from_profiles
from src.periferia.gui.window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _window(app, tmp_path: Path, config_text: str) -> MainWindow:
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(config_text, encoding="utf-8")
    return MainWindow(path)


def test_builds_without_a_config(app, tmp_path):
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    window = MainWindow(tmp_path / "absent.yaml")
    assert window.windowTitle() == "Periferia"
    assert window.editor.table.rowCount() == 0


def test_shows_the_daemon_state(app, tmp_path):
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    state_mod.write(state_mod.LATCHED, source="PeriferiaMic")
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    assert window.banner.title.text() == "Микрофон залип"


def test_says_so_when_the_daemon_is_down(app, tmp_path):
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    state_mod.clear()
    window = MainWindow(tmp_path / "config.yaml")
    assert window.banner.title.text() == "Демон не запущен"


def test_shows_the_configured_keys(app, tmp_path):
    window = _window(
        app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n  panic_key: KEY_F12\n"
    )
    detail = window.banner.detail.text()
    assert "GRAVE" in detail
    assert "F12" in detail


def test_loads_existing_rows(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
    )
    assert window.profile == "game"
    assert window.editor.current_rows() == [("KEY_CAPSLOCK", "KEY_ESC")]


def test_add_gives_a_valid_row_and_enables_saving(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
    )
    window.editor._add()
    rows = window.editor.current_rows()
    assert len(rows) == 2
    assert rows[0] != rows[1]
    assert window.editor.save_button.isEnabled()


def test_add_stops_when_no_keys_are_left(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    before = window.editor.table.rowCount()
    window.editor._add()
    assert window.editor.table.rowCount() == before + 1


def test_saving_writes_the_file_and_reports_it(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window._save(window.editor.current_rows())
    assert "KEY_CAPSLOCK" in (tmp_path / "config.yaml").read_text()
    assert "Сохранено" in window.statusBar().currentMessage()


def test_saving_an_invalid_table_is_refused(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_A", "KEY_B")])
    window.editor.table.setCellWidget(
        0, 1, window.editor._combo("KEY_LEFTSHIFT")
    )
    window._save(window.editor.current_rows())
    assert "Не сохранено" in window.statusBar().currentMessage()
    assert "profiles" not in (tmp_path / "config.yaml").read_text()


def test_warns_when_ptt_is_being_remapped(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_GRAVE", "KEY_F13")])
    window._save(window.editor.current_rows())
    assert "GRAVE" in window.editor.warning.text()


def test_no_warning_when_ptt_is_untouched(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window._save(window.editor.current_rows())
    assert window.editor.warning.text() == ""


def test_reload_shows_what_was_saved(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window._save(window.editor.current_rows())
    window._load()
    assert rows_from_profiles(window._cfg.profiles) == [("KEY_CAPSLOCK", "KEY_ESC")]


def test_close_stops_the_timer(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.close()
    assert not window._timer.isActive()


def test_shows_the_russian_letter_for_keys(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "ptt:\n  ptt_key: KEY_GRAVE\nprofiles:\n  - name: game\n    remap:\n"
        "      KEY_GRAVE: KEY_F13\n",
    )
    assert window.banner.detail.text().startswith("PTT: Ё (GRAVE)")
    source = window.editor.table.cellWidget(0, 0)
    assert source.currentText() == "Ё (GRAVE)"


def test_picker_shows_letters_but_writes_keycodes(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_GRAVE", "KEY_F13")])
    window._save(window.editor.current_rows())
    text = (tmp_path / "config.yaml").read_text()
    assert "KEY_GRAVE: KEY_F13" in text
    assert "Ё" not in text


def test_picker_can_be_typed_into(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    combo = window.editor.table.cellWidget(0, 0)
    assert combo.isEditable()
    assert combo.insertPolicy() == combo.InsertPolicy.NoInsert
    assert combo.completer().filterMode().name == "MatchContains"
