"""Tests for the active window probe.

The probe cannot be exercised for real in a test run, because it needs a live
KWin. What is worth pinning down is the parsing: KWin's output arrives as a log
line with other text around it, and a parser that is too strict here reports
"no" for a machine that would have answered.
"""

from __future__ import annotations

import subprocess

import pytest

from periferia.core import activewindow, envcheck, windowbus


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


class TestSummarize:
    def _fields(self, **values: str) -> dict:
        return {name: {"kind": "string", "text": v} for name, v in values.items()}

    def test_plain_string_fields_are_understood(self) -> None:
        report = activewindow._summarize(
            {"stage": "basics", "fields": {"caption": "Steam", "resourceClass": None}},
            None,
        )
        assert report.caption == "Steam"
        assert report.interesting() == [("caption", "Steam")]
        assert report.status == envcheck.WARN

    def test_a_baseline_survives_a_dead_extras_pass(self) -> None:
        report = activewindow._summarize(
            {"stage": "basics", "fields": {"resourceClass": "steam"}}, None
        )
        assert report.matchable() == ["resourceClass"]

    def test_the_error_stage_is_reported(self) -> None:
        report = activewindow._summarize(
            {"stage": "error", "fields": {"message": "no active client"}}, None
        )
        assert "no active client" in report.detail

    def test_resource_class_makes_it_a_match(self) -> None:
        report = activewindow._summarize(
            {"fields": self._fields(caption="Steam", resourceClass="steam")}, None
        )
        assert report.status == envcheck.OK
        assert report.matchable() == ["resourceClass"]

    def test_caption_alone_is_not_enough(self) -> None:
        report = activewindow._summarize({"fields": self._fields(caption="Steam")}, None)
        assert report.status == envcheck.WARN
        assert report.matchable() == []

    def test_interesting_skips_empty_fields(self) -> None:
        report = activewindow._summarize(
            {
                "fields": {
                    "caption": {"kind": "string", "text": "Steam"},
                    "resourceClass": {"kind": "null"},
                }
            },
            None,
        )
        assert report.interesting() == [("caption", "Steam")]

    def test_silent_never_overlaps_interesting(self) -> None:
        """The two lists were once derived separately and came to disagree."""
        report = activewindow._summarize(
            {
                "fields": {
                    "caption": "Konsole",
                    "resourceClass": "org.kde.konsole",
                    "windowRole": None,
                }
            },
            None,
        )
        named = [name for name, _ in report.interesting()]
        assert not set(named) & set(report.silent())
        assert set(named) | set(report.silent()) == set(report.fields)

    def test_compositor_error_is_passed_through(self) -> None:
        report = activewindow._summarize({"error": "no active client"}, None)
        assert "no active client" in report.detail


class _FakeService:
    def __init__(self, reports: list[dict]) -> None:
        self.reports = list(reports)
        self.asked: list[float] = []

    def next_report(self, timeout: float) -> dict | None:
        self.asked.append(timeout)
        return self.reports.pop(0) if self.reports else None


class TestAwaitWindow:
    def test_a_hello_alone_means_the_script_is_alive(self) -> None:
        service = _FakeService([{"stage": "hello", "fields": {"ok": "1"}}])
        report, alive = activewindow._await_window(service, 0.2, None)
        assert report is None
        assert alive is True

    def test_silence_means_the_script_never_ran(self) -> None:
        report, alive = activewindow._await_window(_FakeService([]), 0.05, None)
        assert report is None
        assert alive is False

    def test_a_hello_does_not_count_as_window_data(self) -> None:
        service = _FakeService(
            [
                {"stage": "hello", "fields": {}},
                {"stage": "basics", "fields": {"caption": "Steam"}},
            ]
        )
        report, alive = activewindow._await_window(service, 1.0, None)
        assert report is not None
        assert report["fields"]["caption"] == "Steam"
        assert alive is True

    def test_an_error_stage_comes_straight_back(self) -> None:
        service = _FakeService([{"stage": "error", "fields": {"message": "none"}}])
        report, _ = activewindow._await_window(service, 1.0, None)
        assert report is not None
        assert report["stage"] == "error"


class TestStepCodes:
    def _run(self, code: int, err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, "", err)

    def test_a_clean_step_says_so(self) -> None:
        assert activewindow._step_codes([("start", self._run(0))]) == [
            "start (exit 0): ok"
        ]

    def test_a_failing_step_shows_the_complaint(self) -> None:
        line = activewindow._step_codes([("run", self._run(1, "no such object\n"))])
        assert line == ["run (exit 1): no such object"]


class TestExplain:
    def _failed(self, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", text)

    def test_it_names_the_step_that_failed(self) -> None:
        report = activewindow._explain(self._failed("boom\n"), "start")
        assert report.detail.startswith("start failed")

    def test_an_invalid_path_is_recognised(self) -> None:
        report = activewindow._explain(
            self._failed("Error: /Scripting/Script-1 is not a valid object path"), "run"
        )
        assert "path KWin does not have" in report.detail
        assert "start()" in report.hint

    def test_a_missing_kwin_is_still_its_own_case(self) -> None:
        report = activewindow._explain(
            self._failed("The name is not provided by any .service files")
        )
        assert "not reachable" in report.detail


class TestScriptingMethods:
    def test_method_names_are_read_out_of_introspection(self) -> None:
        xml = '<node><interface name="org.kde.kwin.Scripting">'
        xml += '<method name="loadScript"/><method name="start"/>'
        xml += '<method name="unloadScript"/><signal name="something"/>'
        xml += "</interface></node>"
        assert activewindow._parse_methods(xml) == {
            "loadScript",
            "start",
            "unloadScript",
        }


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


class TestFindInterpreter:
    """Every candidate must be tried, not just the first one.

    The project's own interpreter is tried first and it is a virtualenv without
    system site packages, so it never has PyGObject. Returning on the first
    failure therefore reported the one interpreter least likely to work and
    never reached the system one, which is the whole point of looking.
    """

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
    return activewindow.SCRIPT % {
        "marker": activewindow.MARKER,
        "bus": windowbus.BUS_NAME,
        "path": windowbus.OBJECT_PATH,
        "iface": windowbus.INTERFACE,
    }


class TestScript:
    def test_it_says_hello_before_it_touches_the_window(self) -> None:
        tail = [line.strip() for line in _render().strip().splitlines() if line.strip()]
        hello = next(i for i, line in enumerate(tail) if line.startswith('report("hello"'))
        called = next(i for i, line in enumerate(tail) if line == "send();")
        assert hello < called, tail[hello : called + 1]

    def test_it_does_not_ask_for_names_kwin_has_never_had(self) -> None:
        rendered = _render()
        for gone in ("wm_class", "app_id", "surfaceClass", "windowClass"):
            assert gone not in rendered

    def test_the_fields_it_asks_for_are_the_ones_kwin_answers(self) -> None:
        rendered = _render()
        for name in ("caption", "resourceClass", "resourceName", "windowRole"):
            assert f'read(client, "{name}")' in rendered

    def test_script_is_valid_after_substitution(self) -> None:
        rendered = _render()
        assert windowbus.BUS_NAME in rendered
        assert windowbus.OBJECT_PATH in rendered
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
