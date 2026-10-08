"""The window is built with Qt's offscreen platform, so these need no display."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.periferia.core import state as state_mod
from src.periferia.gui.model import first_editable
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
    assert window.editor.keys.table.rowCount() == 0
    assert window.editor.save_button.isEnabled() is False


def test_every_profile_can_be_edited(app, tmp_path):
    """Only the first one used to be reachable, so a profile that names a window
    could be written but not changed."""
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: fallback\n  - name: game\n    match: { resource_class: steam }\n",
    )
    assert window.editor.profile.count() == 2
    window.editor.profile.setCurrentIndex(1)
    window.editor._switched(1)
    assert window.editor.match_class.text() == "steam"


def test_a_fallback_profile_has_no_conditions_to_fill_in(app, tmp_path):
    window = _window(app, tmp_path, "profiles:\n  - name: fallback\n")
    window._new_profile()
    window.editor.no_conditions.setChecked(True)

    assert window.editor.match_class.isEnabled() is False

    window.editor.name.setEditText("game")
    window.editor.match_class.setText("steam")
    window.editor.save()

    assert "match" not in (tmp_path / "config.yaml").read_text()


def test_the_pointer_speed_is_saved_with_the_profile(app, tmp_path):
    window = _window(app, tmp_path, "profiles:\n  - name: fallback\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.speed_touched.setChecked(True)
    window.editor.speed.setValue(2.5)
    window.editor.save()

    text = (tmp_path / "config.yaml").read_text()
    assert "speed: 2.5" in text


def test_a_profile_can_be_deleted(app, tmp_path):
    window = _window(
        app, tmp_path, "profiles:\n  - name: fallback\n  - name: game\n"
    )
    window.editor.profile.setCurrentIndex(1)
    window.editor._switched(1)

    window.editor._delete()

    assert "game" not in (tmp_path / "config.yaml").read_text()
    assert "удалён" in window.statusBar().currentMessage()


def test_macros_inside_a_profile_survive_editing_it(app, tmp_path):
    """The window does not show them, so it has no business deleting them."""
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n"
        "    macros:\n      - name: hello\n        bind: KEY_F5\n",
    )
    window.editor.keys.set_rows([("KEY_CAPSLOCK", "KEY_TAB")])
    window.editor.save()

    text = (tmp_path / "config.yaml").read_text()
    assert "hello" in text
    assert "KEY_TAB" in text


def test_shows_the_daemon_state(app, tmp_path):
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    state_mod.write(state_mod.LATCHED, source="PeriferiaMic")
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    assert window.status_page.big.text() == "Микрофон залип"


def test_says_so_when_the_daemon_is_down(app, tmp_path):
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    state_mod.clear()
    window = MainWindow(tmp_path / "config.yaml")
    assert window.status_page.big.text() == "Демон не запущен"


def test_shows_the_configured_keys(app, tmp_path):
    window = _window(
        app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n  panic_key: KEY_F12\n"
    )
    detail = window.status_page.detail.text()
    assert "GRAVE" in detail
    assert "F12" in detail


def test_loads_existing_rows(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
    )
    assert window.editor.profile.currentData() == "game"
    assert window.editor.keys.current_rows() == [("KEY_CAPSLOCK", "KEY_ESC")]


def test_add_gives_a_valid_row_and_enables_saving(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    remap:\n      KEY_CAPSLOCK: KEY_ESC\n",
    )
    window.editor.keys._add()
    rows = window.editor.keys.current_rows()
    assert len(rows) == 2
    assert rows[0] != rows[1]
    assert window.editor.keys.save_button.isEnabled()


def test_add_stops_when_no_keys_are_left(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    before = window.editor.keys.table.rowCount()
    window.editor.keys._add()
    assert window.editor.keys.table.rowCount() == before + 1


def test_saving_writes_the_file_and_reports_it(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window.editor.save()
    assert "KEY_CAPSLOCK" in (tmp_path / "config.yaml").read_text()
    assert "Сохранено" in window.statusBar().currentMessage()


def test_saving_an_invalid_table_is_refused(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_A", "KEY_B")])
    window.editor.keys.table.setCellWidget(
        0, 1, window.editor.keys._combo("KEY_LEFTSHIFT")
    )
    window.editor.save()
    assert "stuck down" in window.editor.warning.text()
    assert "profiles" not in (tmp_path / "config.yaml").read_text()


def test_says_when_the_microphone_key_is_being_remapped(app, tmp_path):
    """It saves anyway: the router watches the physical key, so the microphone
    still opens. What moves is the desktop's own shortcut."""
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_GRAVE", "KEY_F13")])
    window.editor.save()

    assert "GRAVE" in window.editor.warning.text()
    assert "KEY_F13" in (tmp_path / "config.yaml").read_text()


def test_a_valid_profile_saves_without_a_word_about_it(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window.editor.save()

    assert window.editor.warning.text() == ""
    assert "KEY_CAPSLOCK" in (tmp_path / "config.yaml").read_text()


def test_reload_shows_what_was_saved(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    window.editor.save()
    window._load()
    assert first_editable(window._cfg.profiles).rows == [("KEY_CAPSLOCK", "KEY_ESC")]


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
    assert window.status_page.detail.text().startswith("PTT: Ё (GRAVE)")
    source = window.editor.keys.table.cellWidget(0, 0)
    assert source.currentText() == "Ё (GRAVE)"


def test_picker_shows_letters_but_writes_keycodes(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_profile()
    window.editor.name.setEditText("game")
    window.editor.keys.set_rows([("KEY_GRAVE", "KEY_F13")])
    window.editor.save()
    text = (tmp_path / "config.yaml").read_text()
    assert "KEY_GRAVE: KEY_F13" in text
    assert "Ё" not in text


def test_picker_can_be_typed_into(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window.editor.keys.set_rows([("KEY_CAPSLOCK", "KEY_ESC")])
    combo = window.editor.keys.table.cellWidget(0, 0)
    assert combo.isEditable()
    assert combo.insertPolicy() == combo.InsertPolicy.NoInsert
    assert combo.completer().filterMode().name == "MatchContains"


def test_sidebar_lists_the_questions_in_order(app, tmp_path):
    window = MainWindow(tmp_path / "config.yaml")
    names = [window.nav.item(i).text() for i in range(window.nav.count())]
    assert names == ["Состояние", "Профили", "Устройства", "Правка профиля", "Проверка"]


def test_choosing_a_page_in_the_sidebar_shows_it(app, tmp_path):
    window = MainWindow(tmp_path / "config.yaml")
    assert window.stack.currentWidget() is window.status_page
    window.nav.setCurrentRow(1)
    assert window.stack.currentWidget() is window.profiles_page
    window.nav.setCurrentRow(2)
    assert window.stack.currentWidget() is window.device_page
    window.nav.setCurrentRow(4)
    assert window.stack.currentWidget() is window.diagnostics_page


def test_status_page_names_the_press_to_talk_key_in_russian(app, tmp_path):
    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    assert window.status_page.ptt_value.text() == "Ё (GRAVE)"
    assert window.status_page.detail.text().startswith("PTT: Ё (GRAVE)")


def test_profiles_page_says_what_each_profile_does(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n"
        "  - name: game\n"
        "    match:\n"
        "      resource_class: steam\n"
        "    remap:\n"
        "      KEY_A: KEY_B\n"
        "  - name: rest\n",
    )
    page = window.profiles_page.list
    rows = [page.item(i).text() for i in range(page.count())]
    assert "game" in rows[0]
    assert "resource_class=steam" in rows[0]
    assert "1 переназначений" in rows[0]
    assert "запасной, для всего остального" in rows[1]


def test_profiles_page_marks_a_disabled_profile_rather_than_hiding_it(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n  - name: game\n    enabled: false\n    match:\n      resource_class: steam\n",
    )
    text = window.profiles_page.list.item(0).text()
    assert "выключен" in text


def test_diagnostics_page_shows_the_problems_the_validator_found(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n"
        "  - name: game\n"
        "    match:\n"
        "      clas: steam\n"
        "    remap:\n"
        "      KEY_A: KEY_B\n",
    )
    page = window.diagnostics_page.list
    joined = "\n".join(page.item(i).text() for i in range(page.count()))
    assert "error" in joined
    assert "clas" in joined


def test_diagnostics_page_says_so_when_nothing_is_wrong(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n"
        "  - name: game\n"
        "    match:\n"
        "      resource_class: steam\n"
        "    remap:\n"
        "      KEY_A: KEY_B\n"
        "  - name: rest\n"
        "    remap:\n"
        "      KEY_C: KEY_D\n"
        "devices:\n"
        "  - name: usb\n"
        "    match:\n"
        "      device.bus: usb\n"
        "    audio:\n"
        "      target_volume: 1.0\n",
    )
    assert window.diagnostics_page.list.count() == 1
    assert "Замечаний нет." in window.diagnostics_page.list.item(0).text()


def test_diagnostics_page_warns_when_nothing_catches_other_windows(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "profiles:\n"
        "  - name: game\n"
        "    match:\n"
        "      resource_class: steam\n"
        "    remap:\n"
        "      KEY_A: KEY_B\n",
    )
    page = window.diagnostics_page.list
    joined = "\n".join(page.item(i).text() for i in range(page.count()))
    assert "gets no remap at all" in joined


def test_a_rename_keeps_the_old_name_from_being_duplicated(app, tmp_path):
    """Renaming is a delete plus an add, and the add lands on the row it was
    told to, not on whichever one now has the old name."""
    window = _window(app, tmp_path, "profiles:\n  - name: fallback\n  - name: game\n")
    window.editor.profile.setCurrentIndex(1)
    window.editor._switched(1)
    window.editor.name.setEditText("launcher")
    window.editor.save()

    assert model_profile_names(tmp_path) == ["fallback", "launcher"]


def test_renaming_a_profile_onto_another_one_is_refused(app, tmp_path):
    window = _window(app, tmp_path, "profiles:\n  - name: fallback\n  - name: game\n")
    window.editor.profile.setCurrentIndex(1)
    window.editor._switched(1)
    window.editor.name.setEditText("fallback")
    window.editor.save()

    assert "same name is used twice" in window.editor.warning.text()
    assert model_profile_names(tmp_path) == ["fallback", "game"]


def test_a_profile_whose_speed_was_never_set_does_not_get_one(app, tmp_path):
    """The spin box shows 1.0 whether or not there is a key for it, so reading
    it on its own would put pointer: into every profile."""
    window = _window(app, tmp_path, "profiles:\n  - name: fallback\n")
    window.editor.keys.set_rows([("KEY_A", "KEY_B")])
    window.editor.save()

    assert "pointer" not in (tmp_path / "config.yaml").read_text()


def test_a_profile_can_be_renamed_and_keeps_its_keys(app, tmp_path):
    window = _window(
        app, tmp_path, "profiles:\n  - name: game\n    remap:\n      KEY_A: KEY_B\n"
    )
    window.editor.name.setEditText("launcher")
    window.editor.save()

    text = (tmp_path / "config.yaml").read_text()
    assert model_profile_names(tmp_path) == ["launcher"]
    assert "KEY_A: KEY_B" in text


def test_device_editor_loads_every_entry(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "devices:\n"
        "  - name: usb\n"
        "    match:\n"
        "      device.bus: usb\n"
        "    audio:\n"
        "      target_volume: 1.0\n"
        "  - name: fallback\n",
    )
    editor = window.device_editor
    assert editor.device.count() == 2
    assert editor.match.rows() == [("device.bus", "usb")]
    assert editor.audio.rows() == [("target_volume", "1.0")]


def test_saving_a_device_writes_the_entry(app, tmp_path):
    from src.periferia.core.config import load

    window = _window(app, tmp_path, "ptt:\n  ptt_key: KEY_GRAVE\n")
    window._new_device()
    editor = window.device_editor
    editor.name.setEditText("usb")
    editor.match.set_rows([("device.bus", "usb")])
    editor.audio.set_rows([("target_volume", "1.0"), ("attack_ms", "5")])
    editor.processing.set_rows([("stereo_to_mono", "off")])
    editor.save()

    text = (tmp_path / "config.yaml").read_text()
    assert "device.bus" in text
    assert "target_volume" in text
    device = load(tmp_path / "config.yaml").devices[0]
    assert device.name == "usb"
    assert device.audio == {"target_volume": 1.0, "attack_ms": 5}
    assert device.processing == {"stereo_to_mono": False}


def test_a_number_written_as_text_stays_a_number(app, tmp_path):
    """The window only has text rows, so target_volume typed as "1.4" has to
    come out of the file as the number 1.4, or the strict reader refuses it."""
    from src.periferia.core.config import load

    window = _window(app, tmp_path, "devices:\n  - name: usb\n")
    editor = window.device_editor
    editor.audio.set_rows([("target_volume", "1.4")])
    editor.save()

    assert load(tmp_path / "config.yaml").devices[0].audio == {"target_volume": 1.4}


def test_saving_a_bad_override_value_is_refused(app, tmp_path):
    window = _window(
        app, tmp_path, "devices:\n  - name: usb\n    match:\n      device.bus: usb\n"
    )
    editor = window.device_editor
    editor.audio.set_rows([("target_volume", "loud")])
    editor.save()

    assert "target_volume" in editor.warning.text()
    assert "loud" not in (tmp_path / "config.yaml").read_text()


def test_saving_a_renamed_device_keeps_the_other_entry(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "devices:\n"
        "  - name: analog\n"
        "  - name: usb\n"
        "    match:\n"
        "      device.bus: usb\n",
    )
    editor = window.device_editor
    editor.device.setCurrentIndex(1)
    editor._switched(1)
    editor.name.setEditText("headset")
    editor.save()

    from src.periferia.core.config import load

    assert [d.name for d in load(tmp_path / "config.yaml").devices] == ["analog", "headset"]


def test_deleting_a_device_removes_only_it(app, tmp_path):
    window = _window(
        app,
        tmp_path,
        "devices:\n"
        "  - name: usb\n"
        "    match:\n"
        "      device.bus: usb\n"
        "  - name: fallback\n",
    )
    editor = window.device_editor
    editor.set_draft(editor._drafts[1])
    editor._delete()

    from src.periferia.core.config import load

    assert [d.name for d in load(tmp_path / "config.yaml").devices] == ["usb"]


def model_profile_names(tmp_path: Path) -> list[str]:
    from src.periferia.core.config import load

    return [p.name for p in load(tmp_path / "config.yaml").profiles]
