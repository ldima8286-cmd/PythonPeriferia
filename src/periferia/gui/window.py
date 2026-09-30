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

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Config, load
from ..modules.remap import RemapError
from . import model

log = logging.getLogger("periferia.gui")

POLL_MS = 200

STATE_COLOURS = {
    "OPEN": "#2e7d32",
    "CLOSING": "#9a6700",
    "LATCHED": "#b26a00",
    "PANIC": "#b3261e",
    "CLOSED": "#5f6368",
    "unknown": "#5f6368",
}


class StateBanner(QFrame):
    """The one thing worth seeing from across the room."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("banner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        self.title = QLabel()
        self.title.setObjectName("banner-title")
        self.detail = QLabel()
        self.detail.setObjectName("banner-detail")
        layout.addWidget(self.title)
        layout.addWidget(self.detail)

    def show_status(self, status: model.MicStatus, ptt: str, panic: str) -> None:
        self.title.setText(status.text)
        colour = STATE_COLOURS.get(status.state, STATE_COLOURS["unknown"])
        self.setStyleSheet(
            f"#banner {{ background: {colour}; border-radius: 8px; }}"
            f"#banner-title {{ color: #ffffff; font-size: 20px; font-weight: 600; }}"
            f"#banner-detail {{ color: #eeeeee; font-size: 12px; }}"
        )
        self.detail.setText(status.describe(ptt, panic))


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
            combo.addItem(choice.label, f"KEY_{choice.label}")
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
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


@dataclasses.dataclass
class WindowPaths:
    config: Path


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Periferia")
        self.resize(560, 460)
        self._config_path = config_path or model.config_path()
        self._cfg = _load_or_default(self._config_path)
        self._ptt = model.resolve_label(self._cfg.ptt.ptt_key)
        panic = self._cfg.ptt.panic_key
        self._panic = model.resolve_label(panic) if panic else ""

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        self.banner = StateBanner()
        layout.addWidget(self.banner)

        heading = QLabel("Профиль клавиатуры")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)

        note = QLabel(
            "Переназначение физической клавиши. PTT продолжает работать по исходной "
            "клавише, а рабочий стол увидит новую."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5f6368;")
        layout.addWidget(note)

        self.editor = RemapEditor(on_change=self._save)
        layout.addWidget(self.editor)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        quit_action = QAction("Выход", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

        self._load()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(POLL_MS)

    def _load(self) -> None:
        self.editor.set_rows(model.rows_from_profiles(self._cfg.profiles))
        self.profile = model.profile_name(self._cfg.profiles)

    def _save(self, rows: list[model.Row]) -> None:
        try:
            message = model.check_rows(rows, *_watched_codes(self._cfg))
            model.save_profiles(self._config_path, rows, self.profile)
        except (RemapError, OSError) as exc:
            self.statusBar().showMessage(f"Не сохранено: {exc}", 6000)
            return
        self._cfg = _load_or_default(self._config_path)
        self.statusBar().showMessage("Сохранено. Демон подхватит при перезапуске.", 4000)
        if message:
            self.editor.warning.setText(message)

    def _refresh(self) -> None:
        self.banner.show_status(model.read_status(), self._ptt, self._panic)

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
    print(f"окно построено: {window.windowTitle()!r} / {window.banner.title.text()!r}")
    print(f"строк в редакторе: {window.editor.table.rowCount()}")
