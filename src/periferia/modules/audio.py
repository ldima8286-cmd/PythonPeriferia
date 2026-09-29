"""Virtual microphone and the volume gate that implements PTT.

The physical microphone is never muted. A module-loopback virtual source is
created from it and applications (Discord, Zoom, OBS) are pointed at that
virtual one. PTT then only moves the virtual source's volume, so the
physical device stays available to everything else.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from ..core import pipewire
from ..core.config import AudioConfig

log = logging.getLogger(__name__)

Slider = Callable[[str, float], None]


def _shape(t: float, curve: str) -> float:
    """t is 0..1 progress, returns the gain for that step."""
    if curve == "linear":
        return t
    if curve == "s_curve":
        return t * t * (3.0 - 2.0 * t)
    return 1.0 - math.pow(1.0 - t, 3.0)


def pick_physical_source(preferred: str = "auto") -> str | None:
    """Find the real (non virtual, non monitor) capture device."""
    if preferred != "auto":
        if pipewire.is_candidate(preferred):
            return preferred
        log.warning("configured source %s is not a capture device, falling back to auto", preferred)

    for name in pipewire.source_names():
        if pipewire.is_candidate(name):
            return name
    return None


class VirtualMic:
    """Owns the module-loopback source and exposes a smooth volume gate."""

    def __init__(self, cfg: AudioConfig, *, slider: Slider | None = None) -> None:
        self.cfg = cfg
        self._slider = slider or pipewire.set_volume
        self._lock = threading.RLock()
        self._module_id: int | None = None
        self._source: str | None = None
        self._physical: str | None = None
        self._volume = 0.0
        self._ramp_thread: threading.Thread | None = None
        self._held = False

    @property
    def source(self) -> str | None:
        return self._source

    @property
    def physical(self) -> str | None:
        return self._physical

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def is_open(self) -> bool:
        return self._volume > 0.01

    def setup(self) -> str | None:
        """Create the virtual source. Idempotent."""
        with self._lock:
            if self._source and pipewire.source_exists(self._source):
                return self._source

            existing = pipewire.find_source(self.cfg.virtual_name)
            if existing:
                log.info("reusing existing source %s", existing)
                self._source = existing
                self._volume = 0.0
                self._apply(0.0)
                return existing

            physical = pick_physical_source(self.cfg.physical_source)
            if not physical:
                log.error("no physical capture source found")
                return None
            self._physical = physical

            args = [
                f"source={physical}",
                f"source_properties=device.description={self.cfg.virtual_name}",
                "latency_msec=20",
            ]
            module_id = pipewire.load_module("module-loopback", args)
            if module_id is None:
                log.error("module-loopback refused to load")
                return None
            self._module_id = module_id

            self._source = self._find_loopback_source(module_id)
            if not self._source:
                log.error("loopback loaded but its source could not be found")
                return None

            log.info("virtual mic %s <- %s", self._source, physical)
            self._apply(0.0)
            return self._source

    def _find_loopback_source(self, module_id: int) -> str | None:
        return pipewire.source_by_module(module_id)

    def _apply(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        try:
            self._slider(self._source or "", self._volume)
        except pipewire.PipeWireError as exc:
            log.warning("volume update failed: %s", exc)

    def ramp(self, target: float, duration_ms: int) -> None:
        """Move to target over duration_ms, cancelling any ramp in flight."""
        with self._lock:
            self._cancel_ramp_locked()

            if duration_ms <= 0 or target == self._volume:
                self._apply(target)
                return

            start = self._volume
            steps = max(2, int(duration_ms / 5))
            interval = duration_ms / 1000.0 / steps
            curve = self.cfg.curve
            slider = self._slider
            source = self._source or ""

            def worker() -> None:
                started = time.monotonic()
                for step in range(1, steps + 1):
                    progress = min(1.0, (step / steps))
                    value = start + (target - start) * _shape(progress, curve)
                    try:
                        slider(source, max(0.0, min(1.0, value)))
                    except pipewire.PipeWireError:
                        break
                    remaining = started + interval * step - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                with self._lock:
                    self._volume = target

            self._ramp_thread = threading.Thread(target=worker, name="periferia-ramp", daemon=True)
            self._ramp_thread.start()

    def _cancel_ramp_locked(self) -> None:
        thread = self._ramp_thread
        if thread is not None and thread.is_alive():
            self._ramp_thread = None
        self._ramp_thread = None

    def open_mic(self) -> None:
        self._held = True
        self.ramp(self.cfg.target_volume, self.cfg.attack_ms)

    def close_mic(self) -> None:
        self._held = False
        self.ramp(0.0, self.cfg.release_ms)

    def panic(self) -> None:
        with self._lock:
            self._held = False
            self._ramp_thread = None
        self._apply(0.0)
        if self._source:
            try:
                pipewire.set_mute(self._source, True)
                pipewire.set_mute(self._source, False)
            except pipewire.PipeWireError:
                pass

    def force_silence(self) -> None:
        self._held = False
        self._apply(0.0)

    def teardown(self) -> None:
        with self._lock:
            self._cancel_ramp_locked()
            if self._module_id is not None:
                pipewire.unload_module(self._module_id)
                self._module_id = None
            self._source = None
