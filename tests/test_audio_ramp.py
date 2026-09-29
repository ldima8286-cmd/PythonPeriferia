from __future__ import annotations

import importlib
import time

import pytest

# Loaded through importlib rather than a plain "from ... import ..." because the
# import statement fails to resolve these submodules on this interpreter, while
# importlib.import_module resolves them every time. Worth revisiting once the
# environment is fixed: the plain form reads better.
config_mod = importlib.import_module("periferia.core.config")
audio_mod = importlib.import_module("periferia.modules.audio")

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
