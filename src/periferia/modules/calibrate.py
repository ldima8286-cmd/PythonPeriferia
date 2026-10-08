"""Channel calibration: which captured channel the mono stage should keep.

The stereo-to-mono stage keeps one of the two captured channels, and the
choice is a property of the card, not of the machine: the band that cancels
out of an application's downmix and which channel survives most intact have
to be measured where the card actually is. This module records a couple of
seconds through both channels, compares them band by band and says whether a
downmix would lose anything and which channel keeps the whole band.

The analyses are written for stdlib only. The bandpass stages are
second-order biquads and the statistics run over frames: one channel being a
few dB quieter or flipped in phase survives that either way, and what matters
here is that the two channels travel the same path, not the exact filter
shape.

The practical picture, measured on the machine this first shipped on: the two
channels were not copies of one mono input, the left channel was louder, and
its coherence with the right channel collapsed above 3.5 kHz with the phase
going towards 180 degrees; (L+R)/2 lost up to 6.4 dB of the 1.5-8 kHz band to
the left channel alone. The mono stage exists because of that.
"""

from __future__ import annotations

import dataclasses
import math
import signal
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

# (lo, hi) in Hz. Wide on purpose: both channels travel the same stage, so a
# coarse passband still keeps the comparison honest.
BANDS = [(40, 300), (300, 900), (900, 1500), (1500, 3500), (3500, 5500), (5500, 9000)]

# Recordings quieter than this have nothing to measure: it sits just above
# room tone once the stage has scaled it.
FLOOR_DB = -60.0

# A pair of channels closer than this in every band is a card that duplicated
# one mono input: a downmix loses nothing there.
CORR_DUP = 0.98
IMBALANCE_DUP = 0.75


def _db(power_ratio: float) -> float:
    return 10.0 * math.log10(max(power_ratio, 1e-12))


class _Bandpass:
    """One second-order RBJ bandpass stage, run sample by sample."""

    __slots__ = ("a1", "a2", "b0", "b1", "b2", "x1", "x2", "y1", "y2")

    def __init__(self, center: float, bandwidth: float, fs: float) -> None:
        w0 = 2.0 * math.pi * center / fs
        q = max(0.1, center / max(bandwidth, 1.0))
        alpha = math.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        self.b0 = alpha / a0
        self.b1 = 0.0
        self.b2 = -alpha / a0
        self.a1 = (-2.0 * math.cos(w0)) / a0
        self.a2 = (1.0 - alpha) / a0
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def step(self, x: float) -> float:
        out = (
            self.b0 * x
            + self.b1 * self.x1
            + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2
        )
        self.x2 = self.x1
        self.x1 = x
        self.y2 = self.y1
        self.y1 = out
        return out


@dataclasses.dataclass(slots=True)
class Band:
    """What one frequency band says about the two channels."""

    lo: int
    hi: int
    left_db: float
    right_db: float
    imbalance_db: float
    corr: float
    mono_l_loss_db: float
    mono_r_loss_db: float


@dataclasses.dataclass(slots=True)
class Result:
    """The whole verdict of a calibration."""

    bands: list[Band]
    copies: bool
    better: str
    worst_loss_db: float
    band_lo: int
    band_hi: int
    silent: bool


def _frame_stats(left: list[float], right: list[float]) -> tuple[float, float, float, float]:
    """Whole-take powers plus a frame-wise correlation of the band.

    The correlation runs over frames because a slow gain drift would push a
    sequence-wide estimate down even for two copies of one signal.
    """
    n = len(left)
    frame = min(2048, max(1, n))
    hop = max(1, frame // 2)
    sum_l2 = sum_r2 = sum_x = 0.0
    corr_sum = 0.0
    corr_n = 0
    i = 0
    while i < n:
        end = min(i + frame, n)
        l2 = r2 = x = 0.0
        for j in range(i, end):
            a = left[j]
            b = right[j]
            l2 += a * a
            r2 += b * b
            x += a * b
        sum_l2 += l2
        sum_r2 += r2
        sum_x += x
        rl = math.sqrt(l2)
        rr = math.sqrt(r2)
        if rl > 0.0 and rr > 0.0:
            corr_sum += x / (rl * rr)
            corr_n += 1
        i += hop
    corr = corr_sum / corr_n if corr_n else 0.0
    return sum_l2, sum_r2, sum_x, corr


def _downmix_loss(sl2: float, sr2: float, sx: float, keep_power: float) -> float:
    """dB an application's (L+R)/2 downmix gives up relative to one channel.

    Computed from the sums rather than from the correlation, so it behaves the
    way a real downmix would under an imbalance. Negative means the downmix is
    quieter: the more negative, the closer a pair of anti-phase channels came
    to cancelling each other out.
    """
    mono = 0.25 * (sl2 + sr2 + 2.0 * sx)
    return _db(mono / max(keep_power, 1e-12))


def analyze(left: list[float], right: list[float], fs: float) -> list[Band]:
    """Compare the two channels band by band.

    `left` and `right` are sample values on any consistent scale; only the
    ratio between the channels matters.
    """
    if not left or not right:
        return [Band(lo, hi, FLOOR_DB, FLOOR_DB, 0.0, 0.0, 0.0, 0.0) for lo, hi in BANDS]
    out: list[Band] = []
    for lo, hi in BANDS:
        center = math.sqrt(lo * hi)
        left_pass = _Bandpass(center, hi - lo, fs)
        right_pass = _Bandpass(center, hi - lo, fs)
        left_f = [left_pass.step(x) for x in left]
        right_f = [right_pass.step(x) for x in right]
        sl2, sr2, sx, corr = _frame_stats(left_f, right_f)
        n = len(left_f)
        left_db = _db(sl2 / n)
        right_db = _db(sr2 / n)
        out.append(
            Band(
                lo=lo,
                hi=hi,
                left_db=left_db,
                right_db=right_db,
                imbalance_db=left_db - right_db,
                corr=corr,
                mono_l_loss_db=_downmix_loss(sl2, sr2, sx, sl2),
                mono_r_loss_db=_downmix_loss(sl2, sr2, sx, sr2),
            )
        )
    return out


def recommend(bands: list[Band]) -> Result:
    """Turn a band table into a verdict and a channel choice.

    `better` is the louder channel across the bands, standing in for the more
    intact one; `worst_loss_db` is how much an application's downmix would
    cost of the best band of the better channel, the place where keeping one
    channel shows the most.
    """
    even = all(
        abs(b.imbalance_db) < IMBALANCE_DUP and b.corr > CORR_DUP for b in bands
    )
    if even:
        return Result(
            bands=bands,
            copies=True,
            better="left",
            worst_loss_db=0.0,
            band_lo=0,
            band_hi=0,
            silent=False,
        )

    left = sum(b.left_db for b in bands)
    right = sum(b.right_db for b in bands)
    better = "left" if left >= right else "right"
    losses = [b.mono_l_loss_db if better == "left" else b.mono_r_loss_db for b in bands]
    worst = min(range(len(losses)), key=lambda i: losses[i])
    return Result(
        bands=bands,
        copies=False,
        better=better,
        worst_loss_db=losses[worst],
        band_lo=bands[worst].lo,
        band_hi=bands[worst].hi,
        silent=False,
    )


def is_silent(bands: list[Band]) -> bool:
    return max(b.left_db for b in bands) < FLOOR_DB


def yaml_entry(source: str, better: str) -> str:
    """The devices registry entry the calibration recommends for `source`."""
    mono_from = "front-right" if better == "right" else "front-left"
    name = Path(source).name
    return (
        "devices:\n"
        f"  - name: {name}\n"
        "    match:\n"
        f"      node.name: {source}\n"
        "    processing:\n"
        "      stereo_to_mono: true\n"
        f"      mono_from: {mono_from}\n"
        "    audio:\n"
        "      target_volume: 1.4\n"
    )


def format_report(source: str, result: Result, seconds: float, rate: int) -> str:
    rows = [f"recording {source} ({seconds:.1f} s @ {rate} Hz)"]
    rows.append("")
    rows.append(
        f"{'band (Hz)':<12} {'L dB':>6} {'R dB':>6} {'L-R':>6} {'corr':>6} "
        f"{'mono-L':>7} {'mono-R':>7}"
    )
    for b in result.bands:
        rows.append(
            f"{f'{b.lo}-{b.hi}':<12} {b.left_db:6.1f} {b.right_db:6.1f} "
            f"{b.imbalance_db:+6.1f} {b.corr:6.2f} "
            f"{b.mono_l_loss_db:7.1f} {b.mono_r_loss_db:7.1f}"
        )
    rows.append("")
    rows.extend(verdict(source, result))
    return "\n".join(rows)


def verdict(source: str, result: Result) -> list[str]:
    if result.silent:
        return [
            "nothing audible above the noise floor.",
            "speak or play something, or hand the card a tone with --chirp.",
        ]
    if result.copies:
        return [
            "the two channels are copies of one mono input: an application",
            "downmixing them loses nothing, so the mono stage is optional.",
        ]
    rows = [
        "the two channels are not copies of one mono input: an application",
        "downmixing them gives up "
        f"{abs(result.worst_loss_db):.1f} dB of the {result.band_lo}-{result.band_hi} Hz band",
        f"vs the {result.better} channel alone.",
        "",
        "keep one channel so no application reintroduces the cancellation.",
        "Recommended entry (paste into config.yaml):",
    ]
    rows.append(yaml_entry(source, result.better))
    return rows


def read_wav(path: str | Path) -> tuple[list[float], list[float], int]:
    """Decode a PCM wave into (left, right, rate).

    A mono file yields the same channel twice, so the rest of the analysis
    needs no case of its own. The sample is back-scaled to full scale, which
    only trades the numbers a fixed -6 dB or so; ratios are unaffected.
    """
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())

    scale = 2 ** (8 * width - 1)
    if width == 1:
        values: list[int] = list(struct.unpack(f"<{len(raw)}B", raw))
        values = [v - 128 for v in values]
    elif width == 2:
        values = list(struct.unpack(f"<{len(raw) // 2}h", raw))
    elif width == 4:
        values = list(struct.unpack(f"<{len(raw) // 4}i", raw))
    elif width == 3:
        values = []
        for i in range(0, len(raw), 3):
            b0, b1, b2 = raw[i], raw[i + 1], raw[i + 2]
            value = b0 | (b1 << 8) | (b2 << 16)
            if value & 0x800000:
                value -= 0x1000000
            values.append(value)
    else:
        raise ValueError(f"unsupported sample width {width}")

    per_channel = [values[c::channels] for c in range(channels)]
    left = [v / scale for v in per_channel[0]]
    right = [v / scale for v in (per_channel[1] if channels > 1 else per_channel[0])]
    return left, right, rate


def write_tone(path: str | Path, seconds: float, fs: int = 48000) -> None:
    """A steady multitone through every band, so the analog path has content."""
    n = int(seconds * fs)
    centers = [math.sqrt(lo * hi) for lo, hi in BANDS]
    fade = int(0.25 * fs)
    samples = []
    for i in range(n):
        t = i / fs
        value = sum(0.18 * math.sin(2.0 * math.pi * f * t) for f in centers)
        edge = min(i, n - 1 - i, fade)
        if edge < fade:
            value *= edge / fade
        samples.append(max(-1.0, min(1.0, value)))

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(fs)
        frames = b"".join(struct.pack("<hh", int(v * 32767), int(v * 32767)) for v in samples)
        wav.writeframes(frames)


def _interrupt(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def record_source(
    source: str,
    wav_path: str | Path,
    seconds: float,
    *,
    chirp: bool = False,
    rate: int = 48000,
) -> None:
    """Record both channels for `seconds`, optionally ringing over a tone.

    pw-record waits for something to stop it, so it is told to stop here; the
    tone, when requested, starts first and is stopped after the take so the
    same card pass is measured.
    """
    tone_path: Path | None = None
    playback: subprocess.Popen[Any] | None = None
    if chirp:
        tone_path = Path(wav_path).with_suffix(".tone.wav")
        write_tone(tone_path, seconds, rate)
        playback = subprocess.Popen(
            ["pw-play", str(tone_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    recorder = subprocess.Popen(
        [
            "pw-record",
            "--format", "s16",
            "--rate", str(rate),
            "--channels", "2",
            "--target", source,
            str(wav_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(seconds)
    finally:
        _interrupt(recorder)
        if playback is not None:
            _interrupt(playback)
        if tone_path is not None:
            tone_path.unlink(missing_ok=True)