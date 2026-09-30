"""Tests for the active window probe.

The probe cannot be exercised for real in a test run, because it needs a live
KWin. What is worth pinning down is the parsing: KWin's output arrives as a log
line with other text around it, and a parser that is too strict here reports
"no" for a machine that would have answered.
"""

from __future__ import annotations

import pytest

from periferia.core import activewindow


def _line(payload: str) -> str:
    return f"js: {activewindow.MARKER} {payload}"


class TestParseMarker:
    def test_plain_json(self) -> None:
        found = activewindow._parse_marker(_line('{"caption": "Discord"}'))
        assert found is not None
        assert found["caption"] == "Discord"

    def test_surrounded_by_other_log_lines(self) -> None:
        log = "\n".join(
            [
                "kwin_wayland[1234]: js: kwin script starting",
                _line('{"resourceClass": "steam"}'),
                "kwin_wayland[1234]: something else entirely",
            ]
        )
        found = activewindow._parse_marker(log)
        assert found is not None
        assert found["resourceClass"] == "steam"

    def test_ignores_event_lines(self) -> None:
        log = "\n".join(
            [
                _line('{"event": "clientActivated"}'),
                _line('{"resourceName": "quake"}'),
            ]
        )
        found = activewindow._parse_marker(log)
        assert found is not None
        assert found == {"resourceName": "quake"}

    def test_no_marker(self) -> None:
        assert activewindow._parse_marker("nothing to see here") is None

    def test_malformed_json_is_skipped_not_fatal(self) -> None:
        log = "\n".join(
            [
                _line("{not json at all"),
                _line('{"resourceName": "doom"}'),
            ]
        )
        found = activewindow._parse_marker(log)
        assert found is not None
        assert found["resourceName"] == "doom"

    def test_empty(self) -> None:
        assert activewindow._parse_marker("") is None


class TestParseInt:
    def test_typical_gdbus_output(self) -> None:
        assert activewindow._parse_int("(3,)") == 3

    def test_plain(self) -> None:
        assert activewindow._parse_int("7") == 7

    def test_with_text(self) -> None:
        assert activewindow._parse_int("id is 12, ok") == 12

    def test_absent(self) -> None:
        assert activewindow._parse_int("nothing") is None


class TestMatchable:
    def _report(self, **kwargs: str) -> activewindow.WindowReport:
        return activewindow.WindowReport("ok", "", **kwargs)

    def test_reports_stable_fields_only(self) -> None:
        report = self._report(
            caption="changing title",
            resource_class="steam",
            resource_name="doom",
        )
        assert report.matchable() == ["resourceClass", "resourceName"]

    def test_caption_alone_is_not_matchable(self) -> None:
        assert self._report(caption="just a title").matchable() == []

    def test_nothing_reported(self) -> None:
        assert self._report().matchable() == []


class TestInspectBus:
    def test_flatpak_proxy_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/flatpak/bus"
        )
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent-for-test")
        report = activewindow.inspect_bus()
        assert report.is_flatpak_proxy is True
        assert report.has_real_socket is False

    def test_plain_address_is_not_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent-for-test")
        report = activewindow.inspect_bus()
        assert report.is_flatpak_proxy is False

    def test_socket_pointing_at_flatpak_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/flatpak")
        report = activewindow.inspect_bus()
        assert report.has_real_socket is False


class TestKwinCoordinates:
    """The D-Bus coordinates, pinned because getting them wrong is silent.

    KWin does not expose a service called org.kde.KWin.Scripting. The scripting
    interface hangs off the org.kde.KWin service at /Scripting, and the
    interface itself is spelled with a lower case k. Every wrong combination of
    those three looks reasonable to type and fails the same way, as a compositor
    that is not running.
    """

    def test_service_is_not_the_scripting_name(self) -> None:
        assert activewindow.KWIN_SERVICE == "org.kde.KWin"

    def test_interface_is_lower_case(self) -> None:
        assert activewindow.SCRIPTING_IFACE == "org.kde.kwin.Scripting"

    def test_object_path(self) -> None:
        assert activewindow.SCRIPTING_PATH == "/Scripting"


class TestBusNames:
    def test_parses_gdbus_listing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        listing = "(['org.freedesktop.DBus', 'org.kde.KWin', 'org.kde.kwin'],)"
        monkeypatch.setattr(
            activewindow,
            "_run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=listing, stderr=""
            ),
        )
        assert activewindow.bus_names() == [
            "org.freedesktop.DBus",
            "org.kde.KWin",
            "org.kde.kwin",
        ]

    def test_failure_yields_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(
            activewindow,
            "_run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="no bus"
            ),
        )
        assert activewindow.bus_names() == []


def _render() -> str:
    from periferia.core._window_receiver import BUS_NAME, INTERFACE, OBJECT_PATH

    return activewindow.SCRIPT % {
        "marker": activewindow.MARKER,
        "bus": BUS_NAME,
        "path": OBJECT_PATH,
        "iface": INTERFACE,
    }


class TestScript:
    def test_script_is_valid_after_substitution(self) -> None:
        rendered = _render()
        from periferia.core._window_receiver import BUS_NAME, OBJECT_PATH

        assert BUS_NAME in rendered
        assert OBJECT_PATH in rendered
        assert rendered.count("{") == rendered.count("}")

    def test_tries_both_kwin_generations(self) -> None:
        rendered = _render()
        assert "activeClient" in rendered
        assert "activeWindow" in rendered

    def test_reports_over_the_bus_not_the_log(self) -> None:
        rendered = _render()
        assert "callDBus" in rendered
        assert "print(" not in rendered

    def test_no_unsubstituted_placeholders(self) -> None:
        assert "%(" not in _render()
