"""The daemon: wires input to audio and holds state."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from types import FrameType

from ..modules.audio import VirtualMic
from ..modules.hotkey import HotkeyListener, pick_device, resolve_key
from ..modules.processing import MicProcessing
from . import config as config_mod
from . import logging_setup

log = logging.getLogger(__name__)

TRAY_CLOSED = 1
TRAY_OPEN = 2
TRAY_PANIC = 3


class Daemon:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.mic = VirtualMic(cfg.audio)
        self.processing = MicProcessing(cfg.processing)
        self._release_at: float | None = None
        self._stop = threading.Event()
        self._listener: HotkeyListener | None = None

    def setup(self) -> bool:
        if not self.cfg.audio.enabled:
            log.info("audio module disabled, nothing to do")
            return False

        physical = self.mic.pick_physical()
        if not physical:
            log.error("no physical capture source found")
            return False

        # Processing has to exist before the loopback is built, otherwise the
        # loopback would copy the raw mic and the noise suppression would go
        # nowhere.
        capture = physical
        if self.cfg.processing.enabled:
            processed = self.processing.start(physical)
            if processed:
                capture = processed
            else:
                log.warning(
                    "mic processing did not start, the virtual mic will carry the raw signal"
                )

        virtual = self.mic.setup(capture_source=capture)
        if not virtual:
            log.error("could not create the virtual microphone")
            self.processing.stop()
            return False

        if self.cfg.audio.start_muted:
            self.mic.force_silence()

        if not self._setup_hotkey():
            log.error("could not set up the hotkey, microphone stays closed")
            self.mic.teardown()
            self.processing.stop()
            return False
        return True

    def _setup_hotkey(self) -> bool:
        if not self.cfg.ptt.enabled:
            log.info("ptt module disabled, use the CLI to open the mic manually")
            return False

        code = resolve_key(self.cfg.ptt.ptt_key)
        if code is None and self.cfg.ptt.ptt_key == "auto":
            log.error(
                "ptt_key is 'auto': run 'periferia pick-key' and paste the result "
                "into the config"
            )
            return False
        if code is None:
            log.error("cannot resolve ptt_key %r", self.cfg.ptt.ptt_key)
            return False

        device = pick_device(self.cfg.ptt.device)
        if device is None:
            log.error("no keyboard device found")
            return False

        panic = resolve_key(self.cfg.ptt.panic_key) if self.cfg.ptt.panic_key else None

        self._listener = HotkeyListener(
            device,
            code,
            panic_code=panic,
            ignore_repeat=self.cfg.ptt.ignore_repeat,
            on_press=self.on_press,
            on_release=self.on_release,
            on_panic=self.on_panic,
        )
        return True

    def on_press(self) -> None:
        log.info("ptt down -> mic open")
        self._release_at = None
        self.mic.open_mic()

    def on_release(self) -> None:
        hold = max(0, self.cfg.audio.hold_ms) / 1000.0
        self._release_at = time.monotonic() + hold

    def on_panic(self) -> None:
        log.warning("panic pressed")
        self._release_at = None
        self.mic.panic()

    def _expire_hold(self) -> None:
        if self._release_at is None:
            return
        if time.monotonic() >= self._release_at:
            self._release_at = None
            self.mic.close_mic()

    def run(self) -> int:
        if not self.setup():
            return 1

        listener = self._listener
        assert listener is not None

        try:
            listener.open()
        except (OSError, RuntimeError) as exc:
            log.error("cannot open the input device: %s", exc)
            log.error("this usually means missing permissions, see docs/udev.md")
            return 1

        log.info("periferia is running, mic %s, press Ctrl+C to stop", self.mic.source)

        try:
            while not self._stop.is_set():
                listener.poll(timeout=0.2)
                self._expire_hold()
        except KeyboardInterrupt:
            pass
        except OSError as exc:
            log.error("input loop ended: %s", exc)
            return 1
        finally:
            listener.close()
            self.mic.force_silence()
            self.processing.stop()
            self.mic.teardown()
        return 0

    def stop(self, signum: int, frame: FrameType | None) -> None:
        log.info("signal %s, shutting down", signum)
        self._stop.set()


def main() -> int:
    cfg = config_mod.load()
    logging_setup.setup(cfg.log)
    daemon = Daemon(cfg)
    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
