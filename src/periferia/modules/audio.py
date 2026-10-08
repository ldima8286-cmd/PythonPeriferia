"""The volume gate that implements PTT.

The physical microphone is never muted. PipeWire's module-echo-cancel is fed
from it and publishes a separate virtual source, which is the one applications
point at. PTT only moves that virtual source's volume, so the physical device
stays available to everything else on the machine:

    physical mic -> module-echo-cancel -> apps (PeriferiaMic)
    physical mic ----------------------> apps (still works, never gated)

A module-loopback used to sit on top of this to rename the result. It was
removed: on WirePlumber 0.5 a loopback loaded through pactl registers a
module id but never creates a node, so the daemon could not start at all. The
echo-cancel module already creates the virtual source, and it accepts
source_properties to name it, so the extra hop bought nothing.

The gate uses set-source-volume rather than muting, because attack and release
are ramps and a mute has nowhere in between.
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

# Nominal spacing of the ramp's writes. The ramp follows the clock, so this
# only decides how often it looks; a write slower than this simply skips ticks.
TICK = 0.005


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
    """Binds a volume gate to a source someone else created."""

    def __init__(
        self,
        cfg: AudioConfig,
        *,
        slider: Slider | None = None,
        rebuild: Callable[[], str | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._slider = slider or pipewire.set_volume
        # Rebuilds the chain that owns the source, for when the source itself
        # is gone. The gate only ever holds a name, so if the node behind that
        # name disappears there is nothing left for it to move.
        self._rebuild = rebuild
        self._lock = threading.RLock()
        self._source: str | None = None
        self._physical: str | None = None
        self._volume = 0.0
        self._ramp_thread: threading.Thread | None = None
        self._ramp_cancel: threading.Event | None = None
        self._held = False
        # Set once a failure has been reported, cleared when one succeeds.
        self._warned = False

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

    def configure(self, cfg: AudioConfig) -> None:
        """Adopt the settings of the device that is in use.

        The gate keeps its source; only the numbers that describe the device
        change, so the next ramp uses them. The physical source is re-resolved
        on the next pick, in case the profile changed where to look.
        """
        self.cfg = cfg
        self._physical = None

    def attach(self, source: str | None) -> str | None:
        """Bind the gate to an already created source.

        This class no longer builds any PipeWire module. The source comes from
        MicProcessing, which publishes the echo-cancel output under the name
        applications look for. Loading a module-loopback here looked reasonable
        but never produced a node on WirePlumber 0.5.
        """
        with self._lock:
            if not source:
                return None
            self._source = source
            self._volume = 0.0
            self._apply(0.0)
            log.info("virtual mic %s ready", source)
            return source

    def _apply(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        if not self._source:
            return
        # A name that stopped resolving is worth one retry. PipeWire renames the
        # echo-cancel output when the module is reloaded, so the handle captured
        # at startup can go stale while the source itself is alive and well.
        for attempt in (0, 1):
            try:
                self._slider(self._source, self._volume)
            except pipewire.PipeWireError as exc:
                if attempt == 0 and self._reresolve():
                    continue
                self._report(exc)
                return
            else:
                if attempt:
                    log.info("volume is driving %s again", self._source)
                self._warned = False
                return

    def _reresolve(self) -> bool:
        """Get a source that still exists, one way or another.

        The description in the config is the only handle that outlives a
        reload, so ask for it first. If nothing carries that description then
        the node is genuinely gone, and the owner of the chain has to build it
        again. Reporting the same refusal on every key press helps nobody.
        """
        found = pipewire.find_source(self.cfg.virtual_name)
        if found and found != self._source:
            log.info("source is now %s, was %s", found, self._source)
            self._source = found
            return True
        if found or self._rebuild is None:
            return False
        rebuilt = self._rebuild()
        if rebuilt and rebuilt != self._source:
            log.info("rebuilt the source as %s, was %s", rebuilt, self._source)
            self._source = rebuilt
            return True
        return False

    def _report(self, exc: pipewire.PipeWireError) -> None:
        """Say it once, then keep quiet.

        Every press of the key drove two of these, and a condition that never
        resolves will not fix itself between presses. Repeating it only pushes
        the failures that matter out of the journal.
        """
        if self._held and not self._warned:
            log.warning("volume update failed: %s (further attempts go quiet)", exc)
            self._warned = True
        else:
            log.debug("volume update failed: %s", exc)

    def ramp(self, target: float, duration_ms: int) -> None:
        """Move to target over duration_ms, cancelling any ramp in flight."""
        with self._lock:
            self._cancel_ramp_locked()

            if duration_ms <= 0 or target == self._volume:
                self._apply(target)
                return

            start = self._volume
            duration = duration_ms / 1000.0
            curve = self.cfg.curve
            cancelled = threading.Event()
            self._ramp_cancel = cancelled

            def worker() -> None:
                started = time.monotonic()
                deadline = started + duration
                step = 1
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    # Progress comes from the clock, not from a step counter:
                    # one write against pactl costs far more than a tick, and
                    # counting writes stretched a 200 ms attack into most of a
                    # second. An overdue tick is skipped, never caught up.
                    progress = (now - started) / duration
                    value = start + (target - start) * _shape(progress, curve)
                    with self._lock:
                        if cancelled.is_set():
                            return
                        self._apply(value)
                    while started + step * TICK <= time.monotonic():
                        step += 1
                    wait = min(started + step * TICK, deadline) - time.monotonic()
                    if wait > 0 and cancelled.wait(wait):
                        return
                with self._lock:
                    if cancelled.is_set():
                        return
                    self._apply(target)
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
            self._apply(0.0)
            self._source = None
