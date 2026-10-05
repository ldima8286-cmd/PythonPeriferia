"""Following the focused window, without a compositor.

The bus messages are written out by hand here. That is the whole point of the
split: the part that parses what KWin says and the part that decides which
profile it means are both testable by typing, and neither needs a desktop.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.periferia.core import windowwatch
from src.periferia.core.config import ProfileConfig
from src.periferia.core.windowprofile import Window, select
from src.periferia.modules.remap import remap_for


def _report(**fields: Any) -> str:
    return json.dumps({"stage": "basics", "fields": fields})


def test_a_window_is_read_out_of_what_kwin_sends() -> None:
    window = windowwatch.decode_report(
        _report(resourceClass="steam", resourceName="steam_app_1", caption="Half-Life")
    )
    assert window is not None
    assert window.resource_class == "steam"
    assert window.resource_name == "steam_app_1"
    assert window.caption == "Half-Life"


def test_the_scripts_own_hello_is_not_mistaken_for_a_window() -> None:
    """The script reports itself before it reports anything else. Reading the
    hello as a window would select a profile for a window that does not exist."""
    assert windowwatch.decode_report(json.dumps({"stage": "hello", "fields": {"ok": "1"}})) is None


def test_a_window_with_nothing_in_it_is_still_a_window() -> None:
    """Focus landing on a panel or a launcher gives nulls. That is a real state,
    and it must not be read as a missing report."""
    window = windowwatch.decode_report(
        json.dumps({"stage": "basics", "fields": {}})
    )
    assert window is not None
    assert window.has_identifier() is False


def test_an_empty_class_is_not_an_identifier() -> None:
    """KWin hands back an empty string for a client it cannot name, and matching
    on that would apply a profile to every unnamed window there is."""
    window = windowwatch.decode_report(_report(resourceClass="", resourceName=""))
    assert window is not None
    assert window.has_identifier() is False


def test_junk_from_the_bus_is_ignored_rather_than_raised() -> None:
    for raw in ("not json", "[]", '"text"', "null", ""):
        assert windowwatch.decode_report(raw) is None


class FakeService:
    def __init__(self, reports=()):
        self.pending = list(reports)

    def next_report(self, timeout: float) -> dict[str, Any] | None:
        return self.pending.pop(0) if self.pending else None

    def stop(self) -> None:
        pass


class FakeClock:
    """Time the test moves by hand. Settling is then one line rather than a sleep."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _watcher(reports=()) -> tuple[windowwatch.WindowWatcher, FakeClock]:
    clock = FakeClock()
    watcher = windowwatch.WindowWatcher(clock=clock)
    watcher._service = FakeService(reports)
    return watcher, clock


def test_a_burst_of_window_changes_leaves_the_last_one() -> None:
    """Alt-tab through six windows is six reports. Rebuilding a remap table for
    each one would grab every keyboard six times."""
    watcher, clock = _watcher(
        [{"stage": "basics", "fields": {"resourceClass": f"w{i}"}} for i in range(6)]
    )

    assert watcher.drain() is None  # still settling
    clock.now += 1.0
    window = watcher.drain()

    assert window is not None
    assert window.resource_class == "w5"


def test_a_window_is_held_back_while_it_is_still_changing() -> None:
    """The one case worth waiting on: KWin reports the same switch more than
    once, and each report would otherwise cost the user their keyboard."""
    watcher, clock = _watcher()
    watcher._service = FakeService([{"stage": "basics", "fields": {"resourceClass": "steam"}}])

    watcher.drain()
    clock.now = 0.1  # still inside the settle window
    assert watcher.drain() is None

    watcher._service = FakeService(
        [{"stage": "basics", "fields": {"resourceClass": "steam"}}]
    )
    assert watcher.drain() is None  # same window reported again

    clock.now = 0.5
    window = watcher.drain()
    assert window is not None
    assert window.resource_class == "steam"


def test_the_same_window_is_only_handed_over_once() -> None:
    """Applied twice would rebuild the same table twice, and the daemon would
    have to compare tables to notice."""
    watcher, clock = _watcher()
    clock.now = 1.0
    watcher._service = FakeService([{"stage": "basics", "fields": {"resourceClass": "steam"}}])

    watcher.drain()
    clock.now += 1.0
    assert watcher.drain() is not None
    assert watcher.drain() is None


def test_the_window_already_in_force_is_readable_without_waiting() -> None:
    """A reload has to ask what is focused now, and waiting out the settle on
    every save would make a config edit feel like it did nothing."""
    watcher, clock = _watcher()
    watcher._service = FakeService([{"stage": "basics", "fields": {"resourceClass": "steam"}}])
    watcher.drain()
    clock.now = 10.0
    watcher.drain()

    assert watcher.last is not None
    assert watcher.last.resource_class == "steam"


def test_a_watcher_that_was_never_started_drains_to_nothing() -> None:
    """The daemon asks on every pass, including before the watcher is up."""
    assert windowwatch.WindowWatcher().drain() is None


def test_stop_is_safe_to_call_without_start() -> None:
    """The daemon stops everything on the way out, including a watcher that
    failed to start and left a script name behind."""
    watcher = windowwatch.WindowWatcher()
    watcher.stop()
    watcher.stop()


def test_the_script_is_unloaded_on_the_way_out(monkeypatch) -> None:
    """A script left running in the compositor outlives the daemon and keeps
    reporting to a bus name nobody owns, which makes the next start look broken."""
    calls: list[list[str]] = []

    def fake_gdbus(method: str, *args: str, **_: Any) -> Any:
        calls.append([method, *args])
        return type("Done", (), {"returncode": 0, "stdout": "()", "stderr": ""})()

    monkeypatch.setattr(windowwatch.activewindow, "_gdbus", fake_gdbus)
    watcher = windowwatch.WindowWatcher()
    watcher._script_name = "periferiaWindow123"
    watcher.stop()

    assert calls == [["unloadScript", "periferiaWindow123"]]


def test_no_script_is_loaded_for_a_config_with_a_single_profile() -> None:
    """Nothing decides anything, so nothing is asked of the user's session."""
    assert windowwatch.wants_window([ProfileConfig(name="default")]) is False
    assert windowwatch.wants_window([]) is False


def test_a_profile_that_names_a_window_wants_the_watcher() -> None:
    profiles = [
        ProfileConfig(name="default"),
        ProfileConfig(name="game", match={"resource_class": "steam"}),
    ]
    assert windowwatch.wants_window(profiles) is True


def test_the_daemon_asks_for_the_window_profile_over_the_first_one() -> None:
    """The whole feature, in one pure call: a list of profiles and a window."""
    profiles = [
        ProfileConfig(
            name="default", remap={"KEY_CAPSLOCK": "KEY_ESC"}
        ),
        ProfileConfig(
            name="game",
            match={"resource_class": "steam"},
            remap={"KEY_CAPSLOCK": "KEY_TAB"},
        ),
    ]
    window = Window(resource_class="steam", resource_name="steam_app_1")
    chosen = select(profiles, window)
    assert chosen is not None
    assert chosen.name == "game"
    assert remap_for(chosen) != remap_for(profiles[0])

    elsewhere = Window(resource_class="firefox")
    assert select(profiles, elsewhere).name == "default"


def test_a_window_nothing_claims_falls_back_to_the_nameless_profile() -> None:
    profiles = [
        ProfileConfig(name="game", match={"resource_class": "steam"}),
        ProfileConfig(name="default"),
    ]
    assert select(profiles, Window(resource_class="konsole")).name == "default"


@pytest.mark.parametrize(
    "fields",
    [None, "text", 7],
)
def test_fields_of_the_wrong_type_are_not_a_window(fields: Any) -> None:
    raw = json.dumps({"stage": "basics", "fields": fields})
    assert windowwatch.decode_report(raw) is None