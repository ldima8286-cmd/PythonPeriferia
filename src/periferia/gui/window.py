"""The window itself. Deliberately thin: it draws what the model decided.

Nothing here decides anything. Every rule about what may be remapped lives in
model.py, where it can be tested without a display, and every rule about the
microphone lives in the daemon, which writes the state this only reads.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Config, load
from ..modules.remap import RemapError
from . import model, pages

PAGES = ("Состояние", "Профили", "Устройства", "Правка профиля", "Проверка")

log = logging.getLogger("periferia.gui")

POLL_MS = 200

class RemapEditor(QWidget):
    """From key, to key, and a table of them."""

    def __init__(self, on_change: Callable[[list[model.Row]], None] | None = None) -> None:
        super().__init__()
        self._on_change = on_change
        self._rows: list[model.Row] = []
        self._suspend = False

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Клавиша", "На что заменить"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._edited)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Удалить")
        self.remove_button.clicked.connect(self._remove)
        self.save_button = QPushButton("Сохранить")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b26a00;")
        layout.addWidget(self.warning)

        self._keys = model.available_keys()

    def _combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        for choice in self._keys:
            combo.addItem(choice.label, choice.name)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        # There are several hundred keys and they are not evenly spread over
        # nine letters, so scrolling to find one is not reasonable. Typing any
        # part of the label filters instead. Insert is off, because a
        # free-typed key is not one of the keys the router knows about.
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setDuplicatesEnabled(False)
        completer = combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        return combo

    def set_rows(self, rows: list[model.Row]) -> None:
        self._suspend = True
        self._rows = list(rows)
        self.table.setRowCount(len(rows))
        for row, (source, target) in enumerate(rows):
            self.table.setCellWidget(row, 0, self._combo(source))
            self.table.setCellWidget(row, 1, self._combo(target))
        self._suspend = False
        self._revalidate()

    def current_rows(self) -> list[model.Row]:
        rows: list[model.Row] = []
        for row in range(self.table.rowCount()):
            left = self.table.cellWidget(row, 0)
            right = self.table.cellWidget(row, 1)
            if isinstance(left, QComboBox) and isinstance(right, QComboBox):
                rows.append((str(left.currentData()), str(right.currentData())))
        return rows

    def _edited(self, _item: QTableWidgetItem | None) -> None:
        if not self._suspend:
            self._revalidate()

    def _revalidate(self) -> None:
        rows = self.current_rows()
        ok, reason = model.rows_are_valid(rows)
        self.save_button.setEnabled(ok)
        self.warning.setText("" if ok else reason)

    def _add(self) -> None:
        rows = self.current_rows()
        suggestion = model.suggest_row(rows, self._keys)
        if suggestion is None:
            self.warning.setText("Свободных клавиш не осталось.")
            return
        try:
            rows = model.add_row(rows, *suggestion)
        except RemapError as exc:
            self.warning.setText(str(exc))
            return
        self.set_rows(rows)

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.set_rows(model.remove_row(self.current_rows(), row))

    def _save(self) -> None:
        if self._on_change:
            self._on_change(self.current_rows())


class RowsEditor(QWidget):
    """A two-column text table, for the key/value rows of a device entry.

    The remap editor restricts both columns to known keys. A device entry is
    the opposite: it matches on PipeWire property names and overrides config
    keys, neither of which is a list the window can enumerate, so both columns
    are free text and the file's own validator that decides on save.
    """

    def __init__(self, left_header: str, right_header: str) -> None:
        super().__init__()
        self._suspend = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([left_header, right_header])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.add_button.clicked.connect(self._add)
        self.remove_button = QPushButton("Удалить")
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)

    def set_rows(self, rows: list[model.Row]) -> None:
        self._suspend = True
        self.table.setRowCount(len(rows))
        for row, (key, value) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        self._suspend = False

    def rows(self) -> list[model.Row]:
        out: list[model.Row] = []
        for row in range(self.table.rowCount()):
            left = self.table.item(row, 0)
            right = self.table.item(row, 1)
            if left is not None and right is not None:
                out.append((left.text(), right.text()))
        return out

    def _add(self) -> None:
        self.set_rows([*self.rows(), ("", "")])

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.set_rows([r for i, r in enumerate(self.rows()) if i != row])


class ProfileEditor(QWidget):
    """One profile: which window it applies to, its keys, and its pointer speed.

    A profile is the unit here rather than a key, because a key means nothing on
    its own. Which window it applies to and what it does to the pointer live in
    the same profile and would otherwise need a second window to edit.

    Macros defined inside the profile are shown as a count and not edited. They
    are a recording with its own timing, and a table of key presses cannot ask
    for one, so editing them here would mean editing them somewhere else with a
    second set of rules.
    """

    def __init__(
        self,
        on_save: Callable[[model.ProfileDraft, str | None], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
        on_new: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_new = on_new
        self._original: str | None = None
        self._loading = False
        self._drafts: list[model.ProfileDraft] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        picker = QHBoxLayout()
        self.profile = QComboBox()
        self.profile.currentIndexChanged.connect(self._switched)
        new_button = QPushButton("Новый")
        new_button.clicked.connect(self._new)
        picker.addWidget(QLabel("Профиль"))
        picker.addWidget(self.profile, 1)
        picker.addWidget(new_button)
        layout.addLayout(picker)

        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        layout.addWidget(self.name)

        conditions = QFormLayout()
        self.match_class = _field()
        self.match_name = _field()
        self.match_caption = _field()
        conditions.addRow("Класс окна", self.match_class)
        conditions.addRow("Имя окна", self.match_name)
        conditions.addRow("Заголовок", self.match_caption)
        layout.addLayout(conditions)

        self.no_conditions = QCheckBox("применять везде (запасной профиль)")
        self.no_conditions.toggled.connect(self._conditions_toggled)
        layout.addWidget(self.no_conditions)

        pointer = QHBoxLayout()
        pointer.addWidget(QLabel("Скорость указателя"))
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.05, 10.0)
        self.speed.setSingleStep(0.1)
        self.speed.setDecimals(2)
        self.speed.setValue(1.0)
        self.speed.setEnabled(False)
        self.speed_touched = QCheckBox("менять")
        self.speed_touched.toggled.connect(self.speed.setEnabled)
        pointer.addWidget(self.speed)
        pointer.addWidget(self.speed_touched)
        pointer.addStretch()
        layout.addLayout(pointer)

        self.keys = RemapEditor()
        # Saving belongs to the profile, not to the key table: the table is only
        # one of the three things a save writes.
        self.keys.save_button.setVisible(False)
        layout.addWidget(self.keys, 1)

        row = QHBoxLayout()
        self.delete_button = QPushButton("Удалить профиль")
        self.delete_button.clicked.connect(self._delete)
        self.save_button = QPushButton("Сохранить профиль")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.delete_button)
        row.addStretch()
        row.addWidget(self.save_button)
        layout.addLayout(row)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b26a00;")
        layout.addWidget(self.warning)

    def set_profiles(self, drafts: list[model.ProfileDraft]) -> None:
        self._drafts = list(drafts)
        self._loading = True
        self.profile.blockSignals(True)
        self.profile.clear()
        for draft in drafts:
            self.profile.addItem(draft.name or "без имени", draft.name)
        self.profile.blockSignals(False)
        self._loading = False
        if drafts:
            self.profile.setCurrentIndex(0)
            self.set_draft(drafts[0])
        else:
            self.set_draft(None)

    def set_draft(self, draft: model.ProfileDraft | None) -> None:
        self._loading = True
        self.keys.set_rows(list(draft.rows) if draft else [])
        self.name.blockSignals(True)
        self.name.clear()
        if draft:
            self.name.addItem(draft.name)
            self.name.setCurrentIndex(0)
        self.name.blockSignals(False)
        self.match_class.setText(draft.match.get("resource_class", "") if draft else "")
        self.match_name.setText(draft.match.get("resource_name", "") if draft else "")
        self.match_caption.setText(draft.match.get("caption", "") if draft else "")
        for field in (self.match_class, self.match_name, self.match_caption):
            field.setEnabled(not self.no_conditions.isChecked())
        self.no_conditions.setChecked(not (draft.match if draft else {}))
        self.speed_touched.setChecked(bool(draft and draft.speed is not None))
        self.speed.setValue(float(draft.speed) if draft and draft.speed is not None else 1.0)
        self.delete_button.setEnabled(draft is not None)
        self.save_button.setEnabled(draft is not None)
        self._original = draft.name if draft else None
        self._loading = False

    def _conditions_toggled(self, everywhere: bool) -> None:
        """A fallback profile has no conditions, so the fields are greyed out
        rather than left looking like they still do something."""
        for field in (self.match_class, self.match_name, self.match_caption):
            field.setEnabled(not everywhere)

    def current_draft(self) -> model.ProfileDraft:
        match = {}
        if not self.no_conditions.isChecked():
            for field, widget in (
                ("resource_class", self.match_class),
                ("resource_name", self.match_name),
                ("caption", self.match_caption),
            ):
                if widget.text().strip():
                    match[field] = widget.text().strip()
        chosen = str(self.name.currentText()).strip()
        speed = float(self.speed.value()) if self.speed_touched.isChecked() else None
        return model.ProfileDraft(
            name=chosen or chosen_from(self.profile) or "default",
            match=match,
            rows=self.keys.current_rows(),
            speed=speed,
        )

    def save(self) -> None:
        draft = self.current_draft()
        if self._on_save:
            self._on_save(draft, self._original)

    def _switched(self, index: int) -> None:
        """Show whichever profile was picked.

        Unsaved edits in the one being left are dropped, which is why the save
        button is explicit rather than on every keystroke: switching is the only
        way to lose them, and it says so in the button's place.
        """
        if self._loading or index < 0:
            return
        wanted = str(self.profile.itemData(index) or "")
        for draft in self._drafts:
            if draft.name == wanted:
                self.set_draft(draft)
                return

    def _new(self) -> None:
        if self._on_new:
            self._on_new()

    def _delete(self) -> None:
        if self._on_delete and self._original:
            self._on_delete(self._original)


class DeviceEditor(QWidget):
    """One device entry: which card it matches, and the settings that follow it.

    A match rule is a property the card carries, which nobody can type from
    memory, so `periferia props` on the terminal is the reminder column here:
    it prints the exact property=value pairs a device has. Editing is free text
    and the file's own validator decides on save, with the rule named.
    """

    def __init__(
        self,
        on_save: Callable[[model.DeviceDraft, str | None], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
        on_new: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_new = on_new
        self._original: str | None = None
        self._loading = False
        self._drafts: list[model.DeviceDraft] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        picker = QHBoxLayout()
        self.device = QComboBox()
        self.device.currentIndexChanged.connect(self._switched)
        new_button = QPushButton("Новое")
        new_button.clicked.connect(self._new)
        picker.addWidget(QLabel("Устройство"))
        picker.addWidget(self.device, 1)
        picker.addWidget(new_button)
        layout.addLayout(picker)

        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        layout.addWidget(self.name)

        heading = QLabel("Совпадает с картой (правил может быть несколько, все должны совпасть)")
        heading.setStyleSheet(pages.NOTE_STYLE)
        layout.addWidget(heading)
        self.match = RowsEditor("Свойство", "Значение")
        layout.addWidget(self.match)

        headings = QHBoxLayout()
        heading_a = QLabel("audio:")
        heading_a.setStyleSheet(pages.NOTE_STYLE)
        heading_p = QLabel("processing:")
        heading_p.setStyleSheet(pages.NOTE_STYLE)
        headings.addWidget(heading_a)
        headings.addStretch()
        headings.addWidget(heading_p)
        layout.addLayout(headings)

        sections = QHBoxLayout()
        self.audio = RowsEditor("Параметр", "Значение")
        self.processing = RowsEditor("Параметр", "Значение")
        sections.addWidget(self.audio)
        sections.addWidget(self.processing)
        layout.addLayout(sections)

        row = QHBoxLayout()
        self.delete_button = QPushButton("Удалить устройство")
        self.delete_button.clicked.connect(self._delete)
        self.save_button = QPushButton("Сохранить устройство")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.delete_button)
        row.addStretch()
        row.addWidget(self.save_button)
        layout.addLayout(row)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b26a00;")
        layout.addWidget(self.warning)

    def set_devices(self, drafts: list[model.DeviceDraft]) -> None:
        self._drafts = list(drafts)
        self._loading = True
        self.device.blockSignals(True)
        self.device.clear()
        for draft in drafts:
            self.device.addItem(draft.name or "без имени", draft.name)
        self.device.blockSignals(False)
        self._loading = False
        if drafts:
            self.device.setCurrentIndex(0)
            self.set_draft(drafts[0])
        else:
            self.set_draft(None)

    def set_draft(self, draft: model.DeviceDraft | None) -> None:
        self._loading = True
        self.match.set_rows(list(draft.match) if draft else [])
        self.audio.set_rows(list(draft.audio) if draft else [])
        self.processing.set_rows(list(draft.processing) if draft else [])
        self.name.blockSignals(True)
        self.name.clear()
        if draft:
            self.name.addItem(draft.name)
            self.name.setCurrentIndex(0)
        self.name.blockSignals(False)
        self.delete_button.setEnabled(draft is not None)
        self.save_button.setEnabled(draft is not None)
        self._original = draft.name if draft else None
        self._loading = False

    def current_draft(self) -> model.DeviceDraft:
        return model.DeviceDraft(
            name=str(self.name.currentText()).strip(),
            match=self.match.rows(),
            audio=self.audio.rows(),
            processing=self.processing.rows(),
        )

    def save(self) -> None:
        if self._on_save:
            self._on_save(self.current_draft(), self._original)

    def _switched(self, index: int) -> None:
        if self._loading or index < 0:
            return
        wanted = str(self.device.itemData(index) or "")
        for draft in self._drafts:
            if draft.name == wanted:
                self.set_draft(draft)
                return

    def _new(self) -> None:
        if self._on_new:
            self._on_new()

    def _delete(self) -> None:
        if self._on_delete and self._original:
            self._on_delete(self._original)


def _field() -> QLineEdit:
    """A free-typed text box for one match criterion.

    A profile is matched on what a program calls itself, which nobody can type
    from memory. The list of profiles above therefore doubles as the reminder:
    what other profiles already match on is what can be typed here.
    """
    field = QLineEdit()
    field.setPlaceholderText("например steam")
    return field


def chosen_from(combo: QComboBox) -> str:
    return str(combo.currentData() or "").strip()


@dataclasses.dataclass
class WindowPaths:
    config: Path


class MainWindow(QMainWindow):
    """A sidebar and four pages, in the order they get asked for.

    Which page is showing is the only thing this class decides that matters.
    Status first because it is the question people open this with, validation
    last because it is a question people ask only once something looks wrong.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Periferia")
        self.resize(720, 520)
        self._config_path = config_path or model.config_path()
        self._cfg = _load_or_default(self._config_path)

        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(14, 14, 14, 14)
        shell.setSpacing(16)

        self.nav = QListWidget()
        self.nav.setFixedWidth(148)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        for name in PAGES:
            self.nav.addItem(name)
        shell.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.status_page = pages.StatusPage()
        self.profiles_page = pages.ProfilesPage()
        self.device_page = self._build_devices_page()
        self.keys_page = self._build_keys_page()
        self.diagnostics_page = pages.DiagnosticsPage()
        for page in (
            self.status_page,
            self.profiles_page,
            self.device_page,
            self.keys_page,
            self.diagnostics_page,
        ):
            self.stack.addWidget(page)
        shell.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        quit_action = QAction("Выход", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

        self._load()
        self._refresh()
        self._recheck()
        self.diagnostics_page.recheck.clicked.connect(self._recheck)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(POLL_MS)

    def _build_keys_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel("Правка профиля")
        heading.setStyleSheet(pages.TITLE_STYLE)
        layout.addWidget(heading)
        note = QLabel(
            "Один профиль: в каких окнах он применяется, что он делает с клавишами "
            "и со скоростью указателя. PTT продолжает работать по исходной клавише, "
            "а рабочий стол увидит новую. Макросы внутри профиля здесь не меняются."
        )
        note.setWordWrap(True)
        note.setStyleSheet(pages.NOTE_STYLE)
        layout.addWidget(note)
        self.editor = ProfileEditor(
            on_save=self._save,
            on_delete=self._delete_profile,
            on_new=self._new_profile,
        )
        layout.addWidget(self.editor, 1)
        return page

    def _build_devices_page(self) -> QWidget:
        """A device entry is a card your capture hardware wears. This edits the
        registry of them: which settings go with which card, decided by the
        properties `periferia props` prints for the card in front of you.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel("Устройства")
        heading.setStyleSheet(pages.TITLE_STYLE)
        layout.addWidget(heading)
        note = QLabel(
            "Свойства карты, на которые можно совпадать: `periferia props` показывает "
            "их для микрофона на этой машине, а `periferia sources` — какая запись "
            "устройству достанется. Запись без правил совпадает с любой картой — это "
            "запасная, ей место последней."
        )
        note.setWordWrap(True)
        note.setStyleSheet(pages.NOTE_STYLE)
        layout.addWidget(note)
        self.device_editor = DeviceEditor(
            on_save=self._save_device,
            on_delete=self._delete_device,
            on_new=self._new_device,
        )
        layout.addWidget(self.device_editor, 1)
        return page

    def _load(self) -> None:
        self.editor.set_profiles(model.drafts_from_config(self._cfg.profiles))
        self.device_editor.set_devices(model.drafts_from_devices(self._cfg.devices))

    def _new_profile(self) -> None:
        """An unsaved profile, held in the editor until it is saved.

        Not written on its own: a half-typed profile with a bad key name in it
        would be in the file, and the daemon reads the file, so a name has to be
        given before there is anything to save.
        """
        draft = model.ProfileDraft(name="новый профиль")
        self.editor.set_draft(draft)
        self.editor.keys.set_rows([])
        self.editor.warning.setText(
            "Новый профиль. Задайте имя и нажмите «Сохранить профиль»."
        )

    def _save(self, draft: model.ProfileDraft, previous: str | None) -> None:
        # Only the profile being edited drops out. Leaving the one it is being
        # renamed onto in is the point: two profiles with one name cannot both be
        # picked, and the validator is what says so.
        others = [
            d
            for d in model.drafts_from_config(self._cfg.profiles)
            if d.name != previous
        ]
        problem = model.check_draft(draft, others)
        if problem is not None:
            self.editor.warning.setText(problem)
            return
        try:
            model.save_draft(self._config_path, draft, previous)
        except (RemapError, OSError, ValueError) as exc:
            self.statusBar().showMessage(f"Не сохранено: {exc}", 6000)
            return
        self._cfg = _load_or_default(self._config_path)
        self._load()
        self._recheck()
        # Worth saying after a reload has thrown the editor away, because
        # otherwise the warning only ever showed next to the button that made
        # it, and the button is not what the reader is looking at by then.
        advisory = model.check_rows(draft.rows, *_watched_codes(self._cfg))
        self.editor.warning.setText(advisory or "")
        self.statusBar().showMessage("Сохранено. Демон подхватит при перезапуске.", 4000)

    def _delete_profile(self, name: str) -> None:
        try:
            removed = model.delete_draft(self._config_path, name)
        except OSError as exc:
            self.statusBar().showMessage(f"Не удалено: {exc}", 6000)
            return
        if removed:
            self._cfg = _load_or_default(self._config_path)
            self._load()
            self._recheck()
            self.statusBar().showMessage(f"Профиль {name} удалён.", 4000)

    def _new_device(self) -> None:
        """An unsaved device entry, held in the editor until it is saved."""
        draft = model.DeviceDraft(name="новое устройство")
        self.device_editor.set_draft(draft)
        self.device_editor.warning.setText(
            "Новое устройство. Задайте имя и правила, затем «Сохранить устройство»."
        )

    def _save_device(self, draft: model.DeviceDraft, previous: str | None) -> None:
        others = [
            d
            for d in model.drafts_from_devices(self._cfg.devices)
            if d.name != previous
        ]
        problem = model.check_device_draft(draft, others)
        if problem is not None:
            self.device_editor.warning.setText(problem)
            return
        try:
            model.save_device(self._config_path, draft, previous)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(f"Не сохранено: {exc}", 6000)
            return
        self._cfg = _load_or_default(self._config_path)
        self._load()
        self._recheck()
        self.statusBar().showMessage(
            "Сохранено. Демон применит при следующем переключении устройства.", 4000
        )

    def _delete_device(self, name: str) -> None:
        try:
            removed = model.delete_device(self._config_path, name)
        except OSError as exc:
            self.statusBar().showMessage(f"Не удалено: {exc}", 6000)
            return
        if removed:
            self._cfg = _load_or_default(self._config_path)
            self._load()
            self._recheck()
            self.statusBar().showMessage(f"Устройство {name} удалено.", 4000)

    def _refresh(self) -> None:
        self.status_page.refresh(self._cfg)

    def _recheck(self) -> None:
        """Deliberately not on the timer.

        Rebuilding the problem list twice a second would throw away the scroll
        position of anyone in the middle of reading it.
        """
        self.profiles_page.refresh(self._cfg)
        self.diagnostics_page.refresh(self._cfg)

    def closeEvent(self, event: Any) -> None:
        self._timer.stop()
        super().closeEvent(event)


def _load_or_default(path: Path) -> Config:
    """A missing config is the normal first run, not a failure.

    The window opens against defaults so somebody can look around before they
    have a config at all, and so the first save creates one.
    """
    try:
        return load(path)
    except (FileNotFoundError, ValueError) as exc:
        log.info("starting from defaults: %s", exc)
        return Config()


def _watched_codes(cfg: Config) -> list[int]:
    from ..modules.hotkey import resolve_key

    codes: list[int] = []
    for name in (cfg.ptt.ptt_key, cfg.ptt.panic_key):
        code = resolve_key(name) if name else None
        if code is not None:
            codes.append(code)
    return codes


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def main_headless() -> None:
    """Build the window once and throw it away. Used by the tests."""
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    app.processEvents()
    print(f"окно построено: {window.windowTitle()!r} / {window.status_page.big.text()!r}")
    print(f"строк в редакторе: {window.editor.keys.table.rowCount()}")
