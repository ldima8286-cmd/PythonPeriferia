"""A dot in the panel that says whether the microphone is live.

The daemon already writes down what it is doing, so the tray only has to read
that file. No socket, no second daemon, no way for the two to disagree.

Two shapes are on offer, and they are the same dot:

- the tray icon, which is what a desktop has a place for;
- an overlay, a small frameless window pinned to a corner, for setups where the
  panel is crowded or hidden.

The overlay is click-through where the platform allows it. An indicator that
swallows the clicks meant for the window underneath is worse than no indicator,
and there is nothing to click on it anyway. Wayland ignores the request, so on
Plasma the overlay sits there plainly; it is small and in the corner for that
reason.

Polling rather than watching the file: the note is rewritten on every state
change, and at POLL_MS the cost is a stat() that has nothing to compare against
most of the time.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from . import model

log = logging.getLogger("periferia.tray")

POLL_MS = 200
# The panel asks for 22 px on a normal scale and 44 on a doubled one, so the dot
# is drawn once at a size both can take rather than being scaled up into mush.
DOT_PX = 64
OVERLAY_PX = 32
OVERLAY_MARGIN = 24


def dot_icon(colour: str, *, attention: bool = False, size: int = DOT_PX) -> QIcon:
    """A filled circle in one colour.

    Drawn rather than shipped as six files: the only thing that changes between
    states is a hex string, and six near-identical SVGs would then be six things
    to keep in step with model.STATE_COLOURS.

    Latched and panic get a ring, because those are the two states where the
    microphone is open and nothing about the microphone says so.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = int(size * 0.07) if attention else 0
    if pen:
        painter.setPen(QColor("#ffffff"))
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QRect(pen // 2, pen // 2, size - pen, size - pen))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QRect(0, 0, size - 1, size - 1))
    painter.end()

    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    """The panel icon, plus a menu that does the two things anyone needs."""

    def __init__(self, on_show: Any = None, on_quit: Any = None) -> None:
        super().__init__()
        self._on_show = on_show
        self._on_quit = on_quit
        self._last: model.TrayLook | None = None

        menu = QMenu()
        show = QAction("Окно Periferia", menu)
        show.triggered.connect(self._show)
        menu.addAction(show)
        menu.addSeparator()
        self._state_action = QAction("", menu)
        self._state_action.setEnabled(False)
        menu.addAction(self._state_action)
        menu.addSeparator()
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.setContextMenu(menu)

        # A left click should open the window, which is what every other tray
        # icon does and what people try first. The default, activating on a
        # right click, leaves the window two clicks away.
        self.activated.connect(self._activated)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show()

    def _show(self) -> None:
        if self._on_show:
            self._on_show()

    def _quit(self) -> None:
        if self._on_quit:
            self._on_quit()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def update_status(self, status: model.MicStatus) -> bool:
        """Draw the status. Returns whether anything changed, for the tests."""
        look = model.tray_look(status)
        if look == self._last:
            return False
        self._last = look
        self.setIcon(dot_icon(look.colour, attention=look.attention))
        self.setToolTip(look.text)
        action = self._state_action
        if action is not None:
            action.setText(look.text.partition(": ")[2] or look.text)
        return True

    @property
    def look(self) -> model.TrayLook | None:
        return self._last


class Overlay(QWidget):
    """The same dot, as a window. For when the panel has no room."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(OVERLAY_PX, OVERLAY_PX)
        self._look = model.TrayLook(model.STATE_COLOURS["unknown"], "")

    def update_status(self, status: model.MicStatus) -> bool:
        look = model.tray_look(status)
        if look == self._look:
            return False
        self._look = look
        self.setToolTip(look.text)
        self.update()
        return True

    @property
    def look(self) -> model.TrayLook:
        return self._look

    def place(self) -> None:
        """Bottom right of the screen.

        The available geometry rather than the whole thing, so the dot does not
        land on a panel. Being on the wrong screen is the other way this goes
        wrong, and there is no honest way to follow the focused window without
        the window tracking that is still on the roadmap.
        """
        app = QApplication.instance()
        if app is None:
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - OVERLAY_MARGIN,
            area.bottom() - self.height() - OVERLAY_MARGIN,
        )

    def paintEvent(self, event: Any) -> None:
        # Drawn at three times the widget and scaled down, because the panel and
        # every compositor scale icons themselves and a dot rendered at 32 px has
        # nothing left of its edge after that.
        pixmap = dot_icon(self._look.colour, attention=self._look.attention)
        painter = QPainter(self)
        painter.drawPixmap(
            self.rect(),
            pixmap.pixmap(OVERLAY_PX * 3, OVERLAY_PX * 3),
        )
        painter.end()


class TrayApp:
    """The tray, the overlay, and the timer that keeps them current."""

    def __init__(self, *, overlay: bool = False, show_window: Any = None) -> None:
        self.tray = Tray(on_show=show_window)
        self.overlay = Overlay() if overlay else None
        if not overlay:
            self.tray.show()
        if self.overlay is not None:
            self.overlay.place()
            self.overlay.show()
        self.poll()

        self._timer = QTimer()
        self._timer.timeout.connect(self.poll)
        self._timer.start(POLL_MS)

    def poll(self) -> None:
        status = model.read_status()
        self.tray.update_status(status)
        if self.overlay is not None:
            self.overlay.update_status(status)


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        prog="periferia-tray",
        description="Indicator in the panel showing whether the microphone is open.",
    )
    parser.add_argument(
        "--overlay",
        action="store_true",
        help="a small dot in the corner instead of the panel icon",
    )
    parser.add_argument(
        "--window",
        action="store_true",
        help="also open the Periferia window, and from the tray menu",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = None
    show = None
    if args.window:
        from .window import MainWindow

        window = MainWindow()
        show = window.show
    elif not QSystemTrayIcon.isSystemTrayAvailable() and not args.overlay:
        print(
            "В панели нет места для значка. Запустите с --overlay, "
            "чтобы получить точку в углу экрана.",
            file=sys.stderr,
        )
        return 1

    TrayApp(overlay=args.overlay, show_window=show)
    if window is not None:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())