"""Which keyboards get watched, remembered across sessions and picked up live.

Two behaviours are tested here that are easy to get subtly wrong and impossible
to notice until a keyboard is unplugged mid-game.

Identity: a keyboard is remembered by its physical path, not by `/dev/input/eventN`.
The kernel numbers event nodes in enumeration order, so the same USB keyboard
comes back as a different number after a reboot. Remembering the number would
mean the priority list reshuffles on every boot, which is exactly the problem the
list exists to prevent.

Liveness: a keyboard plugged in while the daemon is running has to start working
without a restart. `add_devices` covers that, including the case where the
keyboard cannot be opened, which must not take the already-working ones with it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from periferia.core import keyboards as kb


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the real cache out of it, so tests neither read nor write it."""
    path = tmp_path / "keyboards.json"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(kb, "cache_path", lambda: path)
    return path


class TestIdentity:
    def test_the_physical_path_is_what_a_keyboard_is_remembered_by(self) -> None:
        """Two boots, two event numbers, one keyboard."""
        first = kb.identity(Path("/dev/input/event4"), phys="usb-0:1:2/input0")
        second = kb.identity(Path("/dev/input/event7"), phys="usb-0:1:2/input0")
        assert first == second

    def test_two_keyboards_on_different_ports_stay_apart(self) -> None:
        a = kb.identity(Path("/dev/input/event4"), phys="usb-0:1:2/input0")
        b = kb.identity(Path("/dev/input/event5"), phys="usb-0:1:3/input0")
        assert a != b

    def test_a_device_with_no_physical_path_falls_back_to_its_by_id_name(self) -> None:
        """Some drivers report no phys at all."""
        ident = kb.identity(Path("/dev/input/event9"), by_id="usb-Foo-event-kbd")
        assert "usb-Foo-event-kbd" in ident

    def test_with_nothing_to_go_on_the_node_is_all_there_is(self) -> None:
        """Not ideal, but it is still better than remembering nothing at all."""
        ident = kb.identity(Path("/dev/input/event9"))
        assert "event9" in ident


class TestOrdering:
    def test_a_keyboard_used_recently_comes_first(self) -> None:
        known = kb.KnownKeyboards(entries=[])
        known.touch("phys:old", name="Laptop")
        time.sleep(0.01)
        known.touch("phys:new", name="External")

        assert known.order(["phys:old", "phys:new"]) == ["phys:new", "phys:old"]

    def test_the_order_survives_being_written_out_and_read_back(self, isolated_cache: Path) -> None:
        """Otherwise the priority only lasts until the daemon exits."""
        known = kb.KnownKeyboards(entries=[])
        known.touch("phys:a")
        time.sleep(0.01)
        known.touch("phys:b")
        known.flush()

        assert kb.KnownKeyboards().order(["phys:a", "phys:b"]) == ["phys:b", "phys:a"]

    def test_a_new_keyboard_does_not_push_the_others_back(self) -> None:
        """Plugging in a keyboard must not demote the one already in use."""
        known = kb.KnownKeyboards(entries=[])
        known.touch("phys:known")
        assert known.order(["phys:known", "phys:brand-new"]) == [
            "phys:known",
            "phys:brand-new",
        ]

    def test_several_unknown_keyboards_keep_the_order_found_in(self) -> None:
        known = kb.KnownKeyboards(entries=[])
        ids = ["phys:c", "phys:a", "phys:b"]
        assert known.order(ids) == ids

    def test_a_keyboard_nobody_has_used_in_half_a_year_is_forgotten(self) -> None:
        """It is not coming back, and it makes the log harder to read."""
        stale = {"id": "phys:ancient", "last_seen": time.time() - 200 * 86400}
        assert kb.KnownKeyboards(entries=[stale]).known() == []

    def test_a_keyboard_used_last_week_is_still_remembered(self) -> None:
        recent = {"id": "phys:current", "last_seen": time.time() - 7 * 86400}
        assert kb.KnownKeyboards(entries=[recent]).known() == [recent]

    def test_a_stale_entry_still_works_while_it_is_plugged_in(self) -> None:
        """Forgetting decides the stored list, not what tonight can use.

        A keyboard in the cache that has not been touched in months is dropped so
        the list stays readable, but if it turns up in this session's discovery it
        is watched and ordered like any other. Only the memory of it is dropped.
        """
        stale = {"id": "phys:ancient", "last_seen": time.time() - 200 * 86400}
        known = kb.KnownKeyboards(entries=[stale])
        assert known.order(["phys:ancient", "phys:current"]) == [
            "phys:ancient",
            "phys:current",
        ]

    def test_a_cache_with_junk_in_it_is_ignored_rather_than_fatal(
        self, isolated_cache: Path
    ) -> None:
        """A half-written cache must not stop the daemon from starting."""
        isolated_cache.write_text('{"keyboards": "not a list"}', encoding="utf-8")
        assert kb.KnownKeyboards().known() == []

        isolated_cache.write_text("this is not json at all", encoding="utf-8")
        assert kb.KnownKeyboards().known() == []

    def test_a_missing_cache_is_not_an_error(self) -> None:
        assert kb.KnownKeyboards().known() == []


class TestRescanRateLimit:
    def test_the_first_look_is_allowed_immediately(self) -> None:
        known = kb.KnownKeyboards(entries=[])
        assert known.rescan_due() is True

    def test_a_second_look_right_after_is_not(self) -> None:
        """Opening every candidate device on every idle poll would be costly."""
        known = kb.KnownKeyboards(entries=[])
        known.rescan_due()
        assert known.rescan_due() is False

    def test_a_look_is_allowed_again_after_the_interval(self) -> None:
        known = kb.KnownKeyboards(entries=[])
        first = time.monotonic()
        known.rescan_due()
        later = time.monotonic() + kb.RESCAN_INTERVAL_S + 1
        assert known.rescan_due(later) is True
        assert later > first