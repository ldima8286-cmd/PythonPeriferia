"""Tests for the daemon's capture-device watch.

The chain is bound to one physical source by name, and nothing rebuilds when
that source is replaced unless the loop notices it: the gate only reacts when
the *virtual* name disappears, which everyday use never triggers. These cover
the watch that asks PipeWire directly.
"""

from __future__ import annotations

import importlib
import time

import pytest

from src.periferia.core import config as config_mod

daemon_mod = importlib.import_module("periferia.core.daemon")
pipewire_mod = importlib.import_module("periferia.core.pipewire")

Daemon = daemon_mod.Daemon


class _Mic:
    source: str | None = "echo-cancel-source"

    def __init__(self, *, is_open: bool = False) -> None:
        self.is_open = is_open
        self.attached: list[str] = []
        self.silenced = False
        self.opened_again = 0
        self.configured: list[object] = []

    def force_silence(self) -> None:
        self.silenced = True
        self.is_open = False

    def attach(self, source: str) -> str:
        self.attached.append(source)
        return source

    def open_mic(self) -> None:
        self.opened_again += 1

    def configure(self, cfg: object) -> None:
        self.configured.append(cfg)


class _Processing:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped = 0
        self.configured: list[object] = []
        self.fail_start = False

    def configure(self, cfg: object) -> None:
        self.configured.append(cfg)

    def start(self, physical: str, *, name: str) -> str | None:
        if self.fail_start:
            return None
        self.started.append((physical, name))
        return "PeriferiaMic-new"

    def stop(self) -> None:
        self.stopped += 1


def _daemon(
    monkeypatch: pytest.MonkeyPatch,
    device: str,
    tag: str,
    *,
    mic_open: bool = False,
) -> tuple[Daemon, _Mic, _Processing]:
    cfg = config_mod.Config()
    daemon = Daemon(cfg)
    mic = _Mic(is_open=mic_open)
    processing = _Processing()
    daemon.mic = mic  # type: ignore[assignment]
    daemon.processing = processing  # type: ignore[assignment]
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: device)
    monkeypatch.setattr(daemon_mod, "device_identity", lambda _p: (device, tag))
    daemon._device_identity = (device, tag)
    daemon._device_watch = True
    daemon._device_failed = False
    daemon._device_checked_at = 0.0
    return daemon, mic, processing


def test_device_identity_is_stable_and_ignores_volume_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    props = {"device.bus": "pci", "audio.channels": "2", "node.volume": "0.9"}
    monkeypatch.setattr(pipewire_mod, "source_props", lambda _name: props)

    before = daemon_mod.device_identity("in")
    assert daemon_mod.device_identity("in") == before
    props["node.volume"] = "0.3"
    assert daemon_mod.device_identity("in") == before, "a level change is not a device change"


def test_device_identity_tracks_a_profile_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    props = {"device.bus": "pci", "audio.channels": "2"}
    monkeypatch.setattr(pipewire_mod, "source_props", lambda _name: props)

    before = daemon_mod.device_identity("in")
    props["audio.channels"] = "1"
    assert daemon_mod.device_identity("in") != before


def test_the_watch_does_nothing_while_the_device_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon, _mic, processing = _daemon(monkeypatch, device="in", tag="same")

    daemon._swap_for_device()

    assert processing.stopped == 0
    assert processing.started == []


def test_a_new_device_rebuilds_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, mic, processing = _daemon(monkeypatch, device="in", tag="old")
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: "usb-headset")
    monkeypatch.setattr(daemon_mod, "device_identity", lambda _p: ("usb-headset", "new"))
    processing.started = []

    daemon._swap_for_device()

    assert processing.stopped == 1, "the old chain has to go"
    assert processing.started == [("usb-headset", "PeriferiaMic")]
    assert mic.attached == ["PeriferiaMic-new"]
    assert daemon._device_identity == ("usb-headset", "new")
    assert processing.configured, "the new device's settings were applied"


def test_an_open_mic_is_reopened_on_the_new_device(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, mic, _processing = _daemon(
        monkeypatch, device="in", tag="old", mic_open=True
    )
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: "usb-headset")
    monkeypatch.setattr(daemon_mod, "device_identity", lambda _p: ("usb-headset", "new"))

    daemon._swap_for_device()

    assert mic.opened_again == 1, "a key held through the plug must keep talking"


def test_no_device_clears_the_claim_so_its_return_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon, _mic, processing = _daemon(monkeypatch, device="in", tag="tag")
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: None)

    daemon._swap_for_device()

    assert daemon._device_identity is None, "the device is gone, nothing claims it"
    assert daemon._device_failed is True, "the return of the device must rebuild"
    assert processing.stopped == 0, "nothing to build onto, so nothing to tear down"


def test_the_return_of_a_device_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, _mic, _processing = _daemon(monkeypatch, device="in", tag="tag")
    daemon._device_identity = None
    daemon._device_failed = True

    daemon._swap_for_device()

    assert daemon._device_identity == ("in", "tag")
    assert daemon._device_failed is False


def test_a_failed_rebuild_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, _mic, processing = _daemon(monkeypatch, device="in", tag="old")
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: "usb-headset")
    monkeypatch.setattr(daemon_mod, "device_identity", lambda _p: ("usb-headset", "new"))
    processing.fail_start = True

    daemon._swap_for_device()

    assert daemon._device_identity is None, "a half-built chain must be retried"
    assert daemon._device_failed is True, "the failure has to stay marked"

    processing.fail_start = False
    daemon._device_checked_at = 0.0
    daemon._swap_for_device()

    assert daemon._device_identity == ("usb-headset", "new")
    assert daemon._device_failed is False


def test_the_watch_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, _mic, processing = _daemon(monkeypatch, device="in", tag="old")
    monkeypatch.setattr(daemon_mod, "pick_physical_source", lambda _pref: "usb-headset")
    monkeypatch.setattr(daemon_mod, "device_identity", lambda _p: ("usb-headset", "new"))
    daemon._device_checked_at = time.monotonic()

    daemon._swap_for_device()

    assert processing.stopped == 0, "the last poll was moments ago"