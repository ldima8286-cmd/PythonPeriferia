"""Virtual microphone and the volume gate that implements PTT.

The physical microphone is never muted. A module-loopback virtual source is
created and applications (Discord, Zoom, OBS) are pointed at that virtual one.
PTT then only moves the virtual source's volume, so the physical device stays
available to everything else.

The loopback copies from the echo-cancel output when processing is on, not from
the raw microphone, so suppression and AEC are part of what gets recorded:

    physical mic -> module-echo-cancel -> module-loopback -> apps
    physical mic ---------------------> module-loopback -> apps
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
        self._ramp_cancel: threading.Event | None = None
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

    def pick_physical(self) -> str | None:
        """Resolve the real capture device without creating anything yet."""
        if self._physical is None:
            self._physical = pick_physical_source(self.cfg.physical_source)
        return self._physical

    def setup(self, capture_source: str | None = None) -> str | None:
        """Create the virtual source. Idempotent.

        capture_source is what the loopback copies from. Normally that is the
        physical microphone, but when mic processing is enabled the daemon
        passes the echo-cancel output instead, so suppression and AEC end up in
        the source applications record.
        """
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

            capture = capture_source or self.pick_physical()
            if not capture:
                log.error("no physical capture source found")
                return None
            if not self._physical:
                self._physical = pick_physical_source(self.cfg.physical_source)

            # module-loopback 1.6 documents source= as the node the virtual
            # source is connected to, and source_output_properties= (not
            # source_properties=, which it does not know) as the way to name
            # that source. Passing the wrong property name made the module
            # register while creating no ports at all.
            args = [
                f"source={capture}",
                f"source_output_properties=device.description={self.cfg.virtual_name}",
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
                log.error(
                    "the module registered but PipeWire never created its ports. "
                    "Check 'pactl list short modules' for the entry and 'pactl list "
                    "short sources' for what it produced; if the loopback is there "
                    "but empty, the capture device it was pointed at is probably "
                    "unavailable, and 'periferia devices' will show what is visible"
                )
                pipewire.unload_module(module_id)
                self._module_id = None
                return None

            if capture == self._physical:
                log.info("virtual mic %s <- %s", self._source, capture)
            else:
                log.info("virtual mic %s <- %s (processed)", self._source, capture)
            self._apply(0.0)
            return self._source

    def _find_loopback_source(self, module_id: int, timeout: float = 2.0) -> str | None:
        """module-loopback creates its ports asynchronously, so right after
        pactl load-module the source is often missing from pactl's output for a
        moment. Give it time to appear instead of failing the whole daemon.
        """
        deadline = time.monotonic() + timeout
        while True:
            found = pipewire.source_by_module(module_id)
            if found:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.15)

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
            cancelled = threading.Event()
            self._ramp_cancel = cancelled

            def worker() -> None:
                started = time.monotonic()
                for step in range(1, steps + 1):
                    if cancelled.is_set():
                        return
                    progress = min(1.0, (step / steps))
                    value = start + (target - start) * _shape(progress, curve)
                    with self._lock:
                        if cancelled.is_set():
                            return
                        self._apply(value)
                    remaining = started + interval * step - time.monotonic()
                    if remaining > 0 and cancelled.wait(remaining):
                        return
                with self._lock:
                    if cancelled.is_set():
                        return
                    self._volume = target
                    self._ramp_cancel = None

            self._ramp_thread = threading.Thread(target=worker, name="periferia-ramp", daemon=True)
            self._ramp_thread.start()

    def _cancel_ramp_locked(self) -> None:
        if self._ramp_cancel is not None:
            self._ramp_cancel.set()
            self._ramp_cancel = None
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
            self._cancel_ramp_locked()
        self._apply(0.0)
        if self._source:
            try:
                pipewire.set_mute(self._source, True)
                pipewire.set_mute(self._source, False)
            except pipewire.PipeWireError:
                pass

    def force_silence(self) -> None:
        self._held = False
        with self._lock:
            self._cancel_ramp_locked()
        self._apply(0.0)

    def teardown(self) -> None:
        with self._lock:
            self._cancel_ramp_locked()
            if self._module_id is not None:
                pipewire.unload_module(self._module_id)
                self._module_id = None
            self._source = None
