"""Mic processing via PipeWire's module-echo-cancel.

No DSP is implemented here. PipeWire already ships RNNoise noise suppression,
acoustic echo cancellation and a voice activity detector, all of which are just
module properties. Turning them on is configuration, not code.
"""

from __future__ import annotations

import logging
import time

from ..core import pipewire
from ..core.config import ProcessingConfig

log = logging.getLogger(__name__)

MODULE = "module-echo-cancel"


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
        self._source: str | None = None

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

        name is the device description to publish. It is applied even when the
        processing itself is disabled, because the point of the module then is
        still to offer a stable, named source for applications to point at.
        """
        if not self.cfg.enabled and not name:
            return None
        if self._module_id is not None:
            return self._source

        props = build_props(self.cfg) if self.cfg.enabled else {}
        if name:
            props["source_properties"] = f"device.description={name}"

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
        log.info("mic processing on %s (module %s)", self._source, module_id)
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
        if self._module_id is not None:
            pipewire.unload_module(self._module_id)
            log.info("mic processing stopped")
            self._module_id = None
            self._source = None
