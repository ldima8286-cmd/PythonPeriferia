from __future__ import annotations

import importlib
import math
import struct
import wave

import pytest

calibrate = importlib.import_module("periferia.modules.calibrate")


def _tone(
    fs: int = 48000, seconds: float = 1.5, freq: float = 1000.0, amp: float = 0.5
) -> list[float]:
    n = int(fs * seconds)
    return [amp * math.sin(2.0 * math.pi * freq * i / fs) for i in range(n)]


def _noise(fs: int = 48000, seconds: float = 1.5, amp: float = 0.05) -> list[float]:
    """Deterministic pseudo noise, so a test never depends on randomness."""
    n = int(fs * seconds)
    value = 0.5
    out = []
    for _ in range(n):
        value = (value * 48271) % 2147483647
        out.append(amp * (value / 1073741823.5 - 1.0))
    return out


def test_identical_channels_are_copies() -> None:
    left = right = [a + b for a, b in zip(_tone(), _noise(), strict=True)]
    result = calibrate.recommend(calibrate.analyze(left, right, 48000))
    assert result.copies is True
    for band in result.bands:
        assert band.corr == pytest.approx(1.0, abs=1e-9)
        assert band.imbalance_db == pytest.approx(0.0, abs=1e-6)
        assert band.mono_l_loss_db == pytest.approx(0.0, abs=1e-6)


def test_an_anti_phase_band_cancels_a_downmix() -> None:
    signal_ = _tone(freq=1200.0)
    noise_ = _noise(amp=0.03)
    left = [s + nn for s, nn in zip(signal_, noise_, strict=True)]
    right = [-s + nn for s, nn in zip(signal_, noise_, strict=True)]

    result = calibrate.recommend(calibrate.analyze(left, right, 48000))

    assert result.copies is False, "a flipped phase in a real band must not read as duplicates"
    band = next(b for b in result.bands if b.lo == 900)
    assert band.corr <= -0.5, "the phase flip has to show up as a negative correlation"
    assert band.mono_l_loss_db <= -10.0, "the downmix cancels the flipped band"


def test_a_quieter_channel_is_measurable() -> None:
    base = [a + b for a, b in zip(_tone(), _noise(amp=0.03), strict=True)]
    left = base
    right = [0.5 * v for v in base]

    result = calibrate.recommend(calibrate.analyze(left, right, 48000))

    assert result.copies is False
    assert result.better == "left"
    band = result.bands[0]
    assert band.imbalance_db == pytest.approx(6.0, abs=0.5), "0.5 scale is -6 dB"


def test_silence_is_detected() -> None:
    left = _noise(amp=0.0001)
    bands = calibrate.analyze(left, left, 48000)
    assert calibrate.is_silent(bands) is True


def test_both_channels_of_a_mono_wav_read_identical(tmp_path) -> None:
    path = tmp_path / "mono.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"".join(struct.pack("<h", i % 2000 - 1000) for i in range(1000)))

    left, right, rate = calibrate.read_wav(path)

    assert rate == 48000
    assert len(left) == 1000
    assert left == right


def test_read_wav_round_trips_s16(tmp_path) -> None:
    raw = bytearray()
    for i in range(500):
        raw += struct.pack("<hh", -1000 + i, 2000 - i)
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(bytes(raw))

    left, right, _ = calibrate.read_wav(path)

    assert left[0] == pytest.approx(-1000 / 32768)
    assert right[0] == pytest.approx(2000 / 32768)


def test_write_tone_is_decodable_and_not_silent(tmp_path) -> None:
    path = tmp_path / "tone.wav"
    calibrate.write_tone(path, 1.0)

    left, right, rate = calibrate.read_wav(path)
    assert rate == 48000
    assert len(left) > 0
    assert calibrate.is_silent(calibrate.analyze(left, right, rate)) is False


class _FakePopen:
    def __init__(self, argv: list[str], **kwargs) -> None:
        self.argv = argv
        self.signals: list[str] = []
        self.returncode = 0

    def poll(self) -> None:
        return None

    def send_signal(self, sig: object) -> None:
        self.signals.append("sig")

    def kill(self) -> None:
        self.signals.append("kill")

    def wait(self, timeout: float = 0.0) -> int:
        return 0


def test_record_source_stops_the_take(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[_FakePopen] = []

    def fake_popen(argv: list[str], **kwargs) -> _FakePopen:
        proc = _FakePopen(argv, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(calibrate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(calibrate.time, "sleep", lambda _s: None)

    calibrate.record_source("some-source", "/tmp/opencode/cal.wav", 3.0)

    assert len(started) == 1
    record = started[0]
    assert record.argv[:4] == ["pw-record", "--format", "s16", "--rate"]
    assert "--target" in record.argv and "some-source" in record.argv
    assert record.signals == ["sig"], "the take has to be closed so the header is written"


def test_record_source_with_chirp_rings_first_and_stops_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[_FakePopen] = []

    def fake_popen(argv: list[str], **kwargs) -> _FakePopen:
        proc = _FakePopen(argv, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(calibrate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(calibrate.time, "sleep", lambda _s: None)

    calibrate.record_source("src", "/tmp/opencode/cal.wav", 2.0, chirp=True)

    assert [p.argv[0] for p in started] == ["pw-play", "pw-record"]
    assert all(p.signals == ["sig"] for p in started)