"""Mic processing via PipeWire's module-echo-cancel.

No DSP is implemented here. PipeWire already ships RNNoise noise suppression,
acoustic echo cancellation and a voice activity detector, all of which are just
module properties. Turning them on is configuration, not code.

Stereo to mono is a small second stage on top: module-echo-cancel always
publishes a stereo source, and its two channels are not duplicates of one mono
input, so an application that downmixes them can cancel the band above 3.5 kHz.
module-remap-source publishes a single mono channel fed by one channel of the
echo-cancel output (processing.mono_from), which no downmix can cancel.
"""

from __future__ import annotations

import logging
import time

from ..core import pipewire
from ..core.config import ProcessingConfig

log = logging.getLogger(__name__)

MODULE = "module-echo-cancel"
REMAP = "module-remap-source"


def build_props(cfg: ProcessingConfig) -> dict[str, str]:
    props: dict[str, str] = {
        "aec_methods": "aec3",
        "aec_tail_length_ms": str(cfg.tail_length_ms),
    }
    if cfg.noise_suppression:
        props["noise_suppression"] = "rnnoise"
        props["noise_suppression_model"] = "rnnoise_builtin"
    if cfg.echo_cancellation:
        props["aec_methods"] = "aec3"
    if cfg.voice_detect:
        props["voice_detect"] = "true"
    props.update(cfg.extra_props)
    return props


class MicProcessing:
    """Loads and unloads the echo-cancel module."""

    def __init__(self, cfg: ProcessingConfig) -> None:
        self.cfg = cfg
        self._module_id: int | None = None
        self._remap_id: int | None = None
        self._source: str | None = None

    def configure(self, cfg: ProcessingConfig) -> None:
        """Adopt the settings of the device that is in use.

        The next start() builds the chain from them; nothing is reloaded here.
        """
        self.cfg = cfg

    @property
    def source(self) -> str | None:
        return self._source

    @property
    def active(self) -> bool:
        return self._module_id is not None

    def start(self, physical_source: str, *, name: str | None = None) -> str | None:
        """Create the named virtual microphone and return its source name.

        The echo-cancel module already produces a virtual source fed by the
        physical microphone, so it is the virtual microphone on its own. A
        module-loopback on top of it was never needed and does not work on
        WirePlumber 0.5, where loading it through pactl registers the module
        without creating any node.

        With stereo_to_mono the echo-cancel source becomes an internal stage
        carrying "<name>-stage", and module-remap-source publishes the name
        applications pick up as a single mono channel fed by the left channel
        of that stage.

        name is the device description to publish. It is applied even when the
        processing itself is disabled, because the point of the module then is
        still to offer a stable, named source for applications to point at.
        """
        if not self.cfg.enabled and not name:
            return None
        if self._module_id is not None:
            return self._source

        mono = self.cfg.stereo_to_mono and bool(name)
        stage = f"{name}-stage" if mono else (name or "")

        # A run under the other setting leaves its node carrying the virtual
        # name too, so clear both module kinds by every name they can have
        # carried, otherwise two sources answer to one name and the gate
        # drives the wrong one.
        for stale_id in pipewire.unload_stale(REMAP, name or ""):
            log.warning(
                "unloaded leftover %s (module %s) from a previous run",
                REMAP,
                stale_id,
            )
        if mono:
            for stale_id in pipewire.unload_stale(MODULE, name or ""):
                log.warning(
                    "unloaded leftover %s (module %s) from a previous run",
                    MODULE,
                    stale_id,
                )
        for stale_id in pipewire.unload_stale(MODULE, stage):
            log.warning(
                "unloaded leftover %s (module %s) from a previous run",
                MODULE,
                stale_id,
            )

        props = build_props(self.cfg) if self.cfg.enabled else {}
        if stage:
            props["source_properties"] = f"device.description={stage}"

        args = [f"source={physical_source}"]
        args += [f"{key}={value}" for key, value in props.items()]

        module_id = pipewire.load_module(MODULE, args)
        if module_id is None:
            log.error("%s refused to load", MODULE)
            return None
        self._module_id = module_id
        self._source = self._find_source(module_id)
        if not self._source:
            log.error("%s loaded but created no source", MODULE)
            pipewire.unload_module(module_id)
            self._module_id = None
            return None
        if not mono:
            log.info("mic processing on %s (module %s)", self._source, module_id)
            log.info("properties: %s", props)
            return self._source

        # The echo-cancel stage must not attenuate what the remap captures,
        # and nothing else touches its volume now that the gate drives the
        # mono source instead.
        pipewire.set_volume(self._source, 1.0)
        remap_id = pipewire.load_module(
            REMAP,
            [
                f"master={self._source}",
                "channels=1",
                # PipeWire picks the master channel by its position. Averaging
                # the two channels instead would keep the cancellation this
                # stage exists to remove.
                f"master_channel_map={self.cfg.mono_from}",
                "channel_map=mono",
                f"source_name={name}",
                f"source_properties=device.description={name}",
            ],
        )
        if remap_id is None:
            log.error("%s refused to load", REMAP)
            pipewire.unload_module(module_id)
            self._module_id = None
            self._source = None
            return None
        self._remap_id = remap_id
        self._source = self._find_source(remap_id)
        if not self._source:
            log.error("%s loaded but created no source", REMAP)
            pipewire.unload_module(remap_id)
            pipewire.unload_module(module_id)
            self._module_id = None
            return None
        log.info(
            "mic processing on %s (module %s, mono via %s)",
            self._source,
            module_id,
            remap_id,
        )
        log.info("properties: %s", props)
        return self._source

    def _find_source(self, module_id: int, timeout: float = 2.0) -> str | None:
        """The module creates its ports asynchronously, so the source is
        missing from pactl's output for a moment after the load returns.
        """
        deadline = time.monotonic() + timeout
        while True:
            found = pipewire.source_by_module(module_id)
            if found:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.15)

    def stop(self) -> None:
        # The remap captures the echo-cancel stage, so it has to go first.
        if self._remap_id is not None:
            pipewire.unload_module(self._remap_id)
            self._remap_id = None
        if self._module_id is not None:
            pipewire.unload_module(self._module_id)
            log.info("mic processing stopped")
            self._module_id = None
            self._source = None
