"""The pages behind the sidebar, each one answering a single question.

The window used to be a banner and a key table, which answers no question in
particular. What a person opens this for is one of three things: is it working,
what does it do to my keys, or what is wrong with what I wrote. So those are
the pages, and nothing else gets a page of its own.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import validate
from . import model

GREY = "#5f6368"
GOOD = "#1e7d32"
BAD = "#b3261e"
WARN = "#8a6d00"

TITLE_STYLE = "font-size: 15px; font-weight: 600;"
NOTE_STYLE = f"color: {GREY};"


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(NOTE_STYLE)
    return label


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(TITLE_STYLE)
    return label


class StatusPage(QWidget):
    """Is it working, and if not, what is in the way."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        banner_layout = QVBoxLayout(self.banner)
        self.big = QLabel()
        self.big.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(NOTE_STYLE)
        banner_layout.addWidget(self.big)
        banner_layout.addWidget(self.detail)
        outer.addWidget(self.banner)

        self.keys_box = QGroupBox("Клавиши")
        grid = QGridLayout(self.keys_box)
        self.ptt_value = QLabel()
        self.panic_value = QLabel()
        self.source_value = QLabel()
        self.source_value.setWordWrap(True)
        grid.addWidget(QLabel("Говорить"), 0, 0)
        grid.addWidget(self.ptt_value, 0, 1)
        grid.addWidget(QLabel("Паника"), 1, 0)
        grid.addWidget(self.panic_value, 1, 1)
        grid.addWidget(QLabel("Источник"), 2, 0)
        grid.addWidget(self.source_value, 2, 1)
        grid.setColumnStretch(1, 1)
        outer.addWidget(self.keys_box)

        self.hint = _note(
            "Микрофон приглушён всегда, кроме момента, пока зажата клавиша. "
            "Обычный микрофон при этом продолжает работать в других программах."
        )
        outer.addWidget(self.hint)
        outer.addStretch(1)

    def refresh(self, cfg: Any) -> None:
        status = model.read_status()
        self.big.setText(status.text)
        if status.open:
            colour = GOOD
        elif status.running:
            colour = WARN
        else:
            colour = BAD
        self.big.setStyleSheet(f"font-size: 19px; font-weight: 600; color: {colour};")
        ptt = model.resolve_label(cfg.ptt.ptt_key)
        panic = model.resolve_label(cfg.ptt.panic_key) if cfg.ptt.panic_key else ""
        self.detail.setText(status.describe(ptt, panic))
        self.ptt_value.setText(ptt)
        self.panic_value.setText(panic or "не назначена")
        self.source_value.setText(status.source or "не найден")


class DiagnosticsPage(QWidget):
    """What is wrong with what was written, before the daemon acts on it."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(_heading("Проверка настроек"))
        outer.addWidget(
            _note(
                "Ошибка означает, что настройка не сработает как задумано. "
                "Предупреждение означает, что сработает не так, как ожидалось."
            )
        )
        self.list = QListWidget()
        self.list.setWordWrap(True)
        outer.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.recheck = QPushButton("Проверить заново")
        row.addWidget(self.recheck)
        row.addStretch(1)
        outer.addLayout(row)

    def refresh(self, cfg: Any) -> None:
        self.list.clear()
        reports = [
            validate.check_profiles(
                cfg.profiles,
                reserved=[cfg.ptt.ptt_key, cfg.ptt.panic_key],
            ),
            validate.check_macros(cfg.macros, cfg.profiles),
            validate.check_devices(cfg.devices),
        ]
        problems = [problem for report in reports for problem in report.problems]
        if not problems:
            item = QListWidgetItem("Замечаний нет.")
            item.setForeground(_colour(GOOD))
            self.list.addItem(item)
            return
        for problem in problems:
            colour = BAD if problem.level == validate.ERROR else WARN
            item = QListWidgetItem(f"{problem.level}: {problem.where}\n{problem.message}")
            item.setForeground(_colour(colour))
            self.list.addItem(item)


def _colour(name: str) -> QColor:
    return QColor(name)


class ProfilesPage(QWidget):
    """The list a person reads to answer "what does this do to my keys"."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(_heading("Профили"))
        outer.addWidget(
            _note(
                "Профиль применяется, когда в фокусе подходящее окно. "
                "Профиль без условий — запасной, для всего остального."
            )
        )
        self.list = QListWidget()
        outer.addWidget(self.list, 1)
        self.empty = _note("Профилей пока нет. Начните с добавления одного.")
        outer.addWidget(self.empty)

    def refresh(self, cfg: Any) -> None:
        self.list.clear()
        profiles = list(cfg.profiles)
        self.empty.setVisible(not profiles)
        if not profiles:
            return
        seen: set[str] = set()
        for index, profile in enumerate(profiles):
            if profile.match:
                match = ", ".join(f"{key}={value}" for key, value in profile.match.items())
            else:
                match = "запасной, для всего остального"
            count = len(profile.remap)
            what = f"{count} переназначений" if count else "ничего не меняет"
            label = f"{profile.name or f'профиль {index + 1}'} — {match}, {what}"
            item = QListWidgetItem(label)
            if not profile.enabled:
                item.setText(f"{label}  (выключен)")
                item.setForeground(_colour(GREY))
            if profile.name in seen:
                item.setForeground(_colour(BAD))
            seen.add(profile.name)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
