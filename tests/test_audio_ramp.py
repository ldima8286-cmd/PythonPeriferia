from __future__ import annotations

import importlib
import logging
import time

import pytest

# Loaded through importlib rather than a plain "from ... import ..." because the
# import statement fails to resolve these submodules on this interpreter, while
# importlib.import_module resolves them every time. Worth revisiting once the
# environment is fixed: the plain form reads better.
config_mod = importlib.import_module("periferia.core.config")
audio_mod = importlib.import_module("periferia.modules.audio")
pipewire_mod = importlib.import_module("periferia.core.pipewire")

AudioConfig = config_mod.AudioConfig
VirtualMic = audio_mod.VirtualMic


def _mic(attack_ms: int = 2000, **kwargs) -> tuple[VirtualMic, list[float]]:
    seen: list[float] = []
    mic = VirtualMic(AudioConfig(attack_ms=attack_ms, **kwargs), slider=lambda s, v: seen.append(v))
    mic._source = "test-source"
    return mic, seen


def test_force_silence_stops_a_running_ramp() -> None:
    # otherwise the ramp keeps writing volume after we tried to shut up
    mic, seen = _mic()
    mic.open_mic()
    time.sleep(0.05)
    mic.force_silence()
    assert mic.volume == 0.0

    time.sleep(0.2)
    assert mic.volume == 0.0, "volume crept back up after force_silence"
    assert seen[-1] == 0.0, "last write to the device was not silence"


def test_reopening_cancels_the_previous_ramp() -> None:
    mic, _seen = _mic()
    mic.open_mic()
    time.sleep(0.03)
    mic.close_mic()
    mic.open_mic()
    time.sleep(0.2)
    assert mic.volume > 0.0, "stale ramp thread overrode the new one"


def test_ramp_thread_finishes() -> None:
    mic, _ = _mic(attack_ms=50)
    mic.open_mic()
    thread = mic._ramp_thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "ramp thread never finished"
    assert mic.volume == pytest.approx(1.0)


def test_a_slow_write_does_not_stretch_the_ramp() -> None:
    # One write against pactl costs a fork and a server round trip, far more
    # than the 5 ms the ramp used to allow per step. Counting steps made a
    # 200 ms attack take the better part of a second, which is heard as the
    # mic opening late. The ramp follows the clock instead.
    seen: list[float] = []

    def slow_slider(_source: str, value: float) -> None:
        time.sleep(0.018)
        seen.append(value)

    mic = VirtualMic(AudioConfig(attack_ms=200), slider=slow_slider)
    mic._source = "test-source"

    started = time.monotonic()
    mic.open_mic()
    thread = mic._ramp_thread
    assert thread is not None
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "ramp thread never finished"
    assert elapsed < 0.4, f"a 200 ms attack took {elapsed:.3f}s"
    assert mic.volume == pytest.approx(1.0)
    assert seen[-1] == pytest.approx(1.0), "the ramp never wrote the final value"
    assert seen == sorted(seen), "the ramp went backwards"


def test_the_ramp_lands_exactly_on_the_target() -> None:
    mic, seen = _mic(attack_ms=60)
    mic.open_mic()
    thread = mic._ramp_thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert seen[-1] == pytest.approx(1.0)
    assert max(seen) == pytest.approx(1.0)


class TestStaleSourceName:
    """PipeWire renames the echo-cancel output when the module is reloaded, so
    the name captured at startup can stop resolving while the source is fine.

    The journal was full of this, twice per press of the key, for hours: the
    daemon held a name the server no longer knew and kept retrying it forever.
    """

    @staticmethod
    def _gone(name: str, fraction: float) -> None:
        raise pipewire_mod.PipeWireError("Нет такого объекта")

    def test_a_renamed_source_is_picked_up_again(self, monkeypatch):
        seen: list[tuple[str, float]] = []
        current = {"name": "echo-cancel-source"}

        def slider(name: str, fraction: float) -> None:
            if name != current["name"]:
                raise pipewire_mod.PipeWireError("Нет такого объекта")
            seen.append((name, fraction))

        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: current["name"])
        mic = VirtualMic(AudioConfig(), slider=slider)
        mic.attach("echo-cancel-source")
        seen.clear()  # attach() zeroes the volume, which is not what is under test
        current["name"] = "echo-cancel-source-renamed"

        mic._held = True
        mic._apply(1.0)

        assert seen == [("echo-cancel-source-renamed", 1.0)]
        assert mic.source == "echo-cancel-source-renamed"

    def test_it_reports_once_for_a_source_that_is_truly_gone(self, monkeypatch, caplog):
        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(AudioConfig(), slider=self._gone)
        mic.attach("echo-cancel-source")
        mic._held = True

        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                mic._apply(1.0)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Нет такого объекта" in warnings[0].message

    def test_recovery_after_a_failure_is_reported_again(self, monkeypatch, caplog):
        fail = {"now": True}

        def slider(name: str, fraction: float) -> None:
            if fail["now"]:
                raise pipewire_mod.PipeWireError("Нет такого объекта")

        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(AudioConfig(), slider=slider)
        mic.attach("echo-cancel-source")
        mic._held = True

        with caplog.at_level(logging.WARNING):
            mic._apply(1.0)
            fail["now"] = False
            mic._apply(1.0)
            fail["now"] = True
            mic._apply(1.0)

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    def test_stays_quiet_while_the_mic_is_not_held(self, monkeypatch, caplog):
        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(AudioConfig(), slider=self._gone)
        mic.attach("echo-cancel-source")

        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                mic._apply(1.0)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestRebuildingAGoneSource:
    """When nothing carries the description any more, the node is gone. Retrying
    the dead name forever is what filled the journal; the owner of the chain has
    to build it again."""

    @staticmethod
    def _gone(name: str, fraction: float) -> None:
        raise pipewire_mod.PipeWireError("Нет такого объекта")

    def test_it_asks_for_a_rebuild_when_the_name_is_unknown(self, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(
            AudioConfig(),
            slider=self._gone,
            rebuild=lambda: (asked.append("yes"), "echo-cancel-source-new")[1],
        )
        mic.attach("echo-cancel-source")
        seen: list[tuple[str, float]] = []
        mic._slider = lambda n, v: seen.append((n, v))

        mic._held = True
        mic._apply(1.0)

        assert asked == ["yes"]
        assert seen == [("echo-cancel-source-new", 1.0)]
        assert mic.source == "echo-cancel-source-new"

    def test_without_a_rebuild_it_reports_and_stops(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(AudioConfig(), slider=self._gone)
        mic.attach("echo-cancel-source")
        mic._held = True

        with caplog.at_level(logging.WARNING):
            mic._apply(1.0)

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_a_failed_rebuild_does_not_raise(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(pipewire_mod, "find_source", lambda d: None)
        mic = VirtualMic(AudioConfig(), slider=self._gone, rebuild=lambda: None)
        mic.attach("echo-cancel-source")
        mic._held = True

        with caplog.at_level(logging.WARNING):
            mic._apply(1.0)  # must not raise

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
