"""Tests for screen lock detection and the forced release it triggers."""

from __future__ import annotations

import subprocess

import pytest

from src.periferia.modules import sessionlock


def test_parses_a_lock_hint_line() -> None:
    line = (
        "/org/freedesktop/login1/session/_32: "
        "org.freedesktop.login1.Session.LockHint: true"
    )
    assert sessionlock.parse_lock_hint(line) is True


def test_parses_unlock() -> None:
    line = (
        "/org/freedesktop/login1/session/_32: "
        "org.freedesktop.login1.Session.LockHint: false"
    )
    assert sessionlock.parse_lock_hint(line) is False


def test_ignores_unrelated_signal_traffic() -> None:
    # gdbus monitor prints every signal on the bus, not just ours
    assert sessionlock.parse_lock_hint("some other object: unrelated: 1") is None
    assert sessionlock.parse_lock_hint("") is None
    assert (
        sessionlock.parse_lock_hint(
            "/org/freedesktop/login1/session/_32: Session.NotLockedHint: true"
        )
        is None
    )


def test_tolerates_a_malformed_value() -> None:
    line = "/x: org.freedesktop.login1.Session.LockHint: <garbage>"
    assert sessionlock.parse_lock_hint(line) is None


def test_session_path_needs_a_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    assert sessionlock.session_object_path() is None


def test_session_path_is_read_from_logind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_ID", "32")
    monkeypatch.setattr(
        sessionlock.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=0, stdout="('/org/freedesktop/login1/session/_32',)", stderr=""
        ),
    )
    # the name has a leading underscore, so it cannot be guessed from the id
    assert sessionlock.session_object_path() == "/org/freedesktop/login1/session/_32"


def test_session_path_is_none_when_logind_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_ID", "32")
    monkeypatch.setattr(
        sessionlock.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=1, stdout="", stderr="no such session"
        ),
    )
    assert sessionlock.session_object_path() is None


def test_start_returns_false_without_gdbus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessionlock, "session_object_path", lambda *a, **k: None)
    watcher = sessionlock.SessionLockWatcher(lambda: None)
    assert watcher.start() is False
    # the daemon must keep running without it, so this has to be safe to call
    watcher.poll()
    watcher.stop()


def test_lock_calls_back_once_per_transition() -> None:
    events: list[str] = []
    watcher = sessionlock.SessionLockWatcher(lambda: events.append("lock"))
    watcher._handle(True)
    watcher._handle(True)  # a repeat must not fire twice
    watcher._handle(False)
    watcher._handle(True)
    assert events == ["lock", "lock"]


def test_degradation_when_the_monitor_dies() -> None:
    events: list[str] = []
    watcher = sessionlock.SessionLockWatcher(lambda: events.append("lock"))
    watcher._handle(True)
    assert events == ["lock"]
    watcher._fail()
    assert watcher.available is False
    # a dead watcher must not raise when the main loop keeps ticking
    watcher.poll()
