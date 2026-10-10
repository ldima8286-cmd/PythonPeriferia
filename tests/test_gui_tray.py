"""The tray icon and the overlay, built with Qt's offscreen platform."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from src.periferia.core import state as state_mod
from src.periferia.gui.model import MicStatus, tray_look
from src.periferia.gui.tray import Overlay, Tray, TrayApp


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def _write_state(path: Path, state: str) -> None:
    state_mod.write(state, source="stub")


def test_an_open_microphone_is_green_and_needs_no_attention():
    look = tray_look(MicStatus(state=state_mod.OPEN, running=True))
    assert look.colour == "#2e7d32"
    assert look.attention is False


def test_a_latched_microphone_is_marked_so_you_can_hear_it_in_a_game():
    """Latched is open with the key released. The tray has to say so, or it
    looks exactly like a healthy open mic."""
    look = tray_look(MicStatus(state=state_mod.LATCHED, running=True))
    assert look.attention is True
    assert look.colour != tray_look(MicStatus(state=state_mod.OPEN, running=True)).colour


def test_a_dead_daemon_is_not_painted_as_a_problem():
    """The machine has a daemon that is not running most of the time the user
    is logged in but not talking. A red dot then would be crying wolf."""
    look = tray_look(MicStatus(state=state_mod.OPEN, running=False))
    assert look.colour == tray_look(MicStatus(state=state_mod.CLOSED, running=False)).colour


def test_the_tray_takes_its_text_from_the_same_source_as_its_colour():
    """One rule, one place. A tray that says 'микрофон закрыт' in green is worse
    than no tray."""
    for state in state_mod.ALL:
        look = tray_look(MicStatus(state=state, running=True))
        assert look.colour.startswith("#")
        assert "Periferia" in look.text


def test_a_fresh_tray_already_has_a_icon(app):
    """A tray shown before any status must not print 'No Icon set'."""
    tray = Tray()
    assert not tray.icon().isNull()
    assert tray.look is None


def test_the_tray_follows_the_daemon(app):
    tray = Tray()
    assert tray.update_status(MicStatus()) is True
    first = tray.look
    assert first is not None
    assert tray.toolTip() == first.text
    assert not tray.icon().isNull()


def test_the_colours_of_every_state_are_distinct_where_they_must_be(app):
    """Open, latched and panic all mean the microphone is live and nobody can
    hear it from outside. They may not look alike."""
    live = {
        tray_look(MicStatus(state=s, running=True)).colour
        for s in (state_mod.OPEN, state_mod.LATCHED, state_mod.PANIC)
    }
    assert len(live) == 3


def test_repainting_an_unchanged_status_is_skipped(app):
    """Polling at 200 ms would otherwise set the icon and the tooltip forever,
    which on some panels makes the entry flicker."""
    tray = Tray()
    status = MicStatus(state=state_mod.OPEN, running=True)
    assert tray.update_status(status) is True
    assert tray.update_status(status) is False
    assert tray.update_status(MicStatus(state=state_mod.CLOSED, running=True)) is True


def test_the_tray_menu_shows_the_state_as_a_read_only_line(app):
    tray = Tray()
    tray.update_status(MicStatus(state=state_mod.LATCHED, running=True))
    action = tray.contextMenu().actions()[2]
    assert action.isEnabled() is False
    assert "залип" in action.text()


def test_the_overlay_sits_inside_the_screen_and_takes_no_clicks(app):
    """A dot in the corner is fine. A dot that eats the clicks meant for the
    window underneath it is worse than no dot at all."""
    overlay = Overlay()
    overlay.place()

    assert screen_contains(app.primaryScreen().availableGeometry(), overlay.geometry())
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    flags = overlay.windowFlags()
    assert flags & Qt.WindowType.WindowTransparentForInput
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint


def screen_contains(area, geometry) -> bool:
    return (
        area.left() <= geometry.left()
        and area.top() <= geometry.top()
        and area.right() >= geometry.right()
        and area.bottom() >= geometry.bottom()
    )


def test_the_app_keeps_both_current_from_one_poll(app, runtime_dir: Path):
    """One read of the state file, not one per thing on screen."""
    _write_state(runtime_dir / "state.json", state_mod.OPEN)
    holder = TrayApp(overlay=True)
    try:
        assert holder.tray.look is not None
        assert holder.tray.look.colour == "#2e7d32"
        assert holder.overlay.look.colour == "#2e7d32"

        _write_state(runtime_dir / "state.json", state_mod.PANIC)
        holder.poll()
        assert holder.tray.look.colour == "#b3261e"
        assert holder.overlay.look.colour == "#b3261e"
    finally:
        holder._timer.stop()
        holder.overlay.close()