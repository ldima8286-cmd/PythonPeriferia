"""A terminal interface for checking that Periferia actually works.

The checks that matter here are about behaviour, not configuration: does the
gate open when the key is held, does panic really bypass hold_ms, does a locked
session close the microphone. Reading a log to answer those is unreliable,
because the log says what was attempted rather than what happened.

This runs the real engine in a background thread and measures the microphone,
so a check is an experiment with a pass or fail, not an opinion. It needs no
dependencies beyond the standard library, which also means it runs over ssh
and inside a container where a GUI would not start.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
from collections import deque
from typing import Any

from .core import config as config_mod
from .core import envcheck, pipewire
from .core.config import Config
from .core.daemon import Daemon
from .modules import hotkey
from .modules.sessionlock import SessionLockWatcher

RESPONDED = 0.05  # level that counts as "the gate moved at all"

ESC = "\x1b"
CSI = "\x1b["
FULL = CSI + "2J"
HOME = CSI + "H"
HIDE = CSI + "?25l"
SHOW = CSI + "?25h"
ALT = CSI + "?1049h"
ALT_OFF = CSI + "?1049l"
RESET = CSI + "0m"
BOLD = CSI + "1m"
DIM = CSI + "2m"
GREEN = CSI + "32m"
YELLOW = CSI + "33m"
RED = CSI + "31m"
CYAN = CSI + "36m"


# ---------------------------------------------------------------- pure helpers


def volume_fraction(source: dict[str, Any] | None) -> float | None:
    """First channel volume as 0..1, or None when the source is gone.

    pactl reports per channel; every channel of our source moves together, so
    the first one is representative and avoids averaging.
    """
    if not source:
        return None
    volume = source.get("volume")
    if not isinstance(volume, dict) or not volume:
        return None
    first = next(iter(volume.values()))
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value) / 65536.0))


def volume_bar(fraction: float | None, width: int = 28) -> str:
    if fraction is None:
        return "?" * width
    filled = round(fraction * width)
    return "#" * filled + "." * (width - filled)


ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def visible_len(text: str) -> int:
    """Length as the terminal draws it, ignoring colour codes.

    Slicing by len() cuts a coloured line short, because the escape bytes are
    counted as if they were visible.
    """
    return len(ANSI.sub("", text))


def fit(text: str, width: int) -> str:
    """Cut to a visible width, leaving the colour codes intact."""
    if visible_len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    out: list[str] = []
    used = 0
    for piece in ANSI.split(text):
        # the split yields an empty piece either side of every escape code
        if not piece:
            continue
        if piece.startswith("\x1b"):
            out.append(piece)
            continue
        take = min(len(piece), width - 1 - used)
        if take <= 0:
            break
        out.append(piece[:take])
        used += take
    line = "".join(out)
    # only reopen a style that was actually open, so plain text stays plain
    return f"{line}{RESET}…" if "\x1b" in line else line + "…"


def level_line(app: App) -> str:
    """The live microphone level, in the middle of the screen, not in a footer.

    Reading it means holding the key and watching this move, which is the whole
    point of the check. Hiding it in the last line meant a terminal that clipped
    the footer hid the only evidence anything was happening.
    """
    value = app.sample_volume()
    if value is None:
        return f"{DIM}live level   {RESET}{YELLOW}no data from pactl{RESET}"
    return (
        f"{DIM}live level   {RESET}{volume_bar(value, 24)} "
        f"{DIM}{value:.0%}{RESET}"
    )


def verdict_line(ok: bool | None, title: str, detail: str = "") -> str:
    """A pass/fail line. None means the check could not decide."""
    if ok is None:
        mark, colour = "? ", YELLOW
    elif ok:
        mark, colour = "+ ", GREEN
    else:
        mark, colour = "x ", RED
    line = f"{colour}{mark}{RESET}{BOLD}{title}{RESET}"
    if detail:
        line += f"  {DIM}{fit(detail, 70)}{RESET}"
    return line


class LogCapture(logging.Handler):
    """Keeps the last log lines so the interface can show them.

    The daemon logs to stderr, which would scribble over the screen, so the
    real handlers are removed and this one takes over.
    """

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        short = record.name.split(".")[-1]
        self.records.append(f"{record.levelname[0]} {short}: {record.getMessage()}")

    def tail(self, count: int) -> list[str]:
        return list(self.records)[-count:]


# --------------------------------------------------------------------- checks


class Check:
    """One experiment. Driven by the main loop, never blocks it."""

    key = "0"
    title = "check"
    hint = "Esc returns to the menu"

    def __init__(self) -> None:
        self.finished = False

    def enter(self, app: App) -> None:
        self.reset()

    # how long a check may wait for the user before it gives up and says so
    timeout = 60.0

    def reset(self) -> None:
        self.finished = False
        self.entered_at = time.monotonic()
        self.timed_out = False

    def update(self, app: App, now: float) -> None:
        return

    def render(self, app: App, width: int) -> list[str]:
        return []

    def on_key(self, app: App, ch: str) -> None:
        if ch == "\x1b":
            app.leave_check()


class CheckEnvironment(Check):
    key = "1"
    title = "Environment"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Environment"
        self.results = envcheck.run_all()
        self.finished = True

    def render(self, app: App, width: int) -> list[str]:
        lines = []
        for item in self.results:
            ok = item.status == envcheck.OK
            none = item.status == envcheck.WARN
            lines.append(verdict_line(ok if not none else None, item.name, item.detail))
            if item.hint and not ok:
                lines.append(f"    {DIM}{fit(item.hint, width - 4)}{RESET}")
        return lines


class CheckDevices(Check):
    key = "2"
    title = "Input devices"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Input devices"
        self.rows: list[tuple[str, str, str]] = []
        ptt = hotkey.resolve_key(app.cfg.ptt.ptt_key) or 0
        want_mouse = hotkey.is_button_code(ptt)
        watched = {
            str(p) for p in hotkey.find_keyboards(app.cfg.ptt.device, include_pointers=want_mouse)
        }
        for path, name in hotkey.list_input_devices():
            try:
                dev = hotkey.open_device(path)
            except OSError as exc:
                self.rows.append((str(path), name, f"blocked: {exc.strerror or exc}"))
                continue
            try:
                if hotkey.is_keyboard(dev):
                    kind = "keyboard"
                elif hotkey.is_pointer(dev):
                    kind = "pointer"
                else:
                    kind = "other"
            finally:
                with contextlib.suppress(OSError):
                    dev.close()
            self.rows.append((str(path), name, kind))
        self.finished = True
        self.watched = watched

    def render(self, app: App, width: int) -> list[str]:
        lines = []
        for path, name, state in self.rows:
            mark = GREEN + "watched" + RESET if path in self.watched else DIM + state + RESET
            lines.append(f"  {fit(path, 22)} {fit(name, 44)} {mark}")
        return lines


class CheckAudio(Check):
    key = "3"
    title = "Audio path"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Audio path"
        self.finished = True
        self.source = app.mic.source
        self.fraction = volume_fraction(pipewire.get_source(self.source)) if self.source else None
        self.desc = ""
        if self.source:
            info = pipewire.get_source(self.source) or {}
            self.desc = str(info.get("description") or "(no description)")
        modules = pipewire.run(["pactl", "list", "modules"], check=False).stdout or ""
        self.echo_cancel = "module-echo-cancel" in modules

    def render(self, app: App, width: int) -> list[str]:
        lines = [
            verdict_line(bool(self.source), "virtual microphone exists", self.source or "missing"),
            verdict_line(
                self.desc == app.cfg.audio.virtual_name,
                "named for applications",
                f"description is {self.desc!r}",
            ),
            verdict_line(self.echo_cancel, "processing module loaded"),
            f"    volume {volume_bar(self.fraction)}",
        ]
        return lines


class CheckProcessing(Check):
    key = "4"
    title = "Mic processing"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Mic processing"
        self.finished = True
        out = pipewire.run(["pactl", "list", "modules"], check=False).stdout or ""
        self.props: dict[str, str] = {}
        wanted = (
            "aec_methods",
            "noise_suppression",
            "voice_detect",
            "aec_tail_length_ms",
        )
        for line in out.splitlines():
            line = line.strip()
            for key in wanted:
                if line.startswith(key + " = "):
                    self.props[key] = line.split("=", 1)[1].strip().strip('"')

    def render(self, app: App, width: int) -> list[str]:
        cfg = app.cfg.processing
        lines = []
        want_aec = "aec3" if cfg.echo_cancellation else "none"
        lines.append(
            verdict_line(
                (self.props.get("aec_methods") or "none") == want_aec,
                "echo cancellation",
                f"aec_methods={self.props.get('aec_methods', 'unset')}",
            )
        )
        ns = self.props.get("noise_suppression", "unset")
        lines.append(
            verdict_line(
                (ns == "rnnoise") == bool(cfg.noise_suppression),
                "noise suppression",
                f"noise_suppression={ns}",
            )
        )
        vad = self.props.get("voice_detect", "unset")
        lines.append(
            verdict_line(
                (vad == "true") == bool(cfg.voice_detect),
                "voice detect",
                f"voice_detect={vad}, config says {cfg.voice_detect}",
            )
        )
        return lines


class CheckGate(Check):
    key = "5"
    title = "Push to talk (hold the key)"

    def reset(self) -> None:
        super().reset()
        self.phase = "idle"
        self.started = 0.0
        self.opened_at = 0.0
        self.attack_ms: float | None = None
        self.held_peak = 0.0
        # only set after seeing real silence, so a microphone that starts
        # open cannot be mistaken for a response to the key
        self.idle_seen = False

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Push to talk"
        app.note = "hold the PTT key for about a second, then release"

    def update(self, app: App, now: float) -> None:
        if self.phase == "done":
            return
        fraction = app.sample_volume()
        if self.phase == "idle":
            if fraction is not None and fraction < 0.02:
                self.idle_seen = True
            elif fraction is not None and fraction >= RESPONDED and self.idle_seen:
                # any movement at all counts as a response, so a gate that only
                # half opens still produces a verdict instead of hanging here
                self.phase = "holding"
                self.started = now
        elif self.phase == "holding":
            if fraction is None:
                return
            self.held_peak = max(self.held_peak, fraction)
            if fraction < 0.02:
                self.phase = "closing"
                self.started = now
        elif self.phase == "closing" and now - self.started > 0.5:
            self.phase = "done"
            self.finished = True

    def render(self, app: App, width: int) -> list[str]:
        if self.phase == "done":
            full = self.held_peak >= app.cfg.audio.target_volume * 0.9
            return [
                verdict_line(
                    full,
                    "gate opened to the target level",
                    f"peak {self.held_peak:.0%} of "
                    f"{app.cfg.audio.target_volume:.0%} target",
                ),
                verdict_line(True, "gate closed on release", "volume returned to 0%"),
                "",
                "Esc returns to the menu.",
            ]
        hint = {
            "idle": "waiting for the key",
            "holding": "key is down, release it",
            "closing": "key released, waiting for the fade out",
        }[self.phase]
        return [
            f"  {BOLD}{hint}{RESET}",
            f"  {level_line(app)}",
            f"  {DIM}the bar has to fill up while you hold "
            f"{app._ptt_label()}{RESET}",
        ]


class CheckPanic(Check):
    """Panic has to be proven by a panic event, not by a quiet microphone.

    A normal release also drops the volume to zero, so watching the level would
    pass even with the panic key unbound. That is worse than no check at all: it
    reports a feature working that does not.
    """

    key = "6"
    title = "Panic key"
    timeout = 90.0

    def reset(self) -> None:
        super().reset()
        self.phase = "need_open"
        self.armed_at = 0.0
        self.drop_ms: float | None = None
        self.closed = False
        self.saw_panic = False

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Panic key"
        app.note = "hold PTT, then press the panic key without letting go of PTT"

    def update(self, app: App, now: float) -> None:
        if self.phase == "done":
            return
        fraction = app.sample_volume()
        open_now = fraction is not None and fraction > 0.5
        if self.phase == "need_open":
            if open_now:
                self.phase = "armed"
                # ignore anything that fired before the hold started
                self.armed_at = time.monotonic()
            return
        if self.phase == "armed":
            if app.daemon is not None and app.daemon.events("panic", self.armed_at):
                self.saw_panic = True
                self.phase = "checking"
            elif not open_now and self.patience_left(app) <= 0.0:
                # the gate closed with no panic behind it: that is a release
                self.phase = "done"
                self.finished = True
            return
        if self.phase == "checking" and fraction is not None and fraction < 0.02:
            self.closed = True
            self.drop_ms = (time.monotonic() - self.armed_at) * 1000.0
            self.phase = "done"
            self.finished = True

    def patience_left(self, app: App) -> float:
        """How long to keep waiting before calling an uneventful hold a miss."""
        return max(0.0, 12.0 - (time.monotonic() - self.armed_at))

    def render(self, app: App, width: int) -> list[str]:
        if self.phase == "need_open":
            return [
                f"  {BOLD}press and hold {app._ptt_label()} first{RESET}",
                f"  {level_line(app)}",
                f"  {DIM}the bar has to fill up before panic can be measured{RESET}",
            ]
        if self.phase == "armed":
            return [
                f"  {BOLD}keep holding, now press {app.cfg.ptt.panic_key}{RESET}",
                f"  {level_line(app)}",
                f"  {DIM}the bar has to drop to zero on its own{RESET}",
            ]
        if not self.saw_panic:
            return [
                verdict_line(
                    False,
                    "no panic event was seen",
                    "the microphone closed, but panic never fired",
                ),
                "",
                f"Panic key is {app.cfg.ptt.panic_key or 'unset'}.",
                "If the volume went to zero, the PTT key was released "
                "instead of the panic key being pressed.",
                "Press 6 again and hold PTT, then press the panic key.",
            ]
        return [
            verdict_line(self.closed, "panic closed the microphone", "volume is 0%"),
            verdict_line(
                self.closed and self.drop_ms is not None and self.drop_ms < 120,
                "closed without waiting for hold_ms",
                f"took {self.drop_ms:.0f} ms, hold_ms is {app.cfg.audio.hold_ms}"
                if self.drop_ms is not None
                else "microphone is still open",
            ),
            "",
            "Esc returns to the menu.",
        ]


class CheckLock(Check):
    key = "7"
    title = "Screen lock"
    timeout = 120.0

    def reset(self) -> None:
        super().reset()
        self.phase = "need_open"
        self.closed_after_lock: bool | None = None
        self.locked_at = 0.0

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Screen lock"
        app.note = (
            "hold PTT, then lock the screen from another terminal. "
            "Do not press any key here, and do not press the panic key: "
            "it would close the microphone for the wrong reason."
        )
        self.watcher = SessionLockWatcher(self._on_lock)
        if not self.watcher.start():
            app.note = "gdbus is unavailable, this check cannot run here"
            self.phase = "done"
            self.finished = True

    def _on_lock(self) -> None:
        self.phase = "locked"
        self.locked_at = time.monotonic()

    def update(self, app: App, now: float) -> None:
        if self.finished:
            return
        if self.watcher is not None:
            self.watcher.poll()
        fraction = app.sample_volume()
        if self.phase == "need_open":
            if fraction is not None and fraction > 0.5:
                self.phase = "holding"
        elif self.phase == "locked":
            # the daemon runs its own watcher, so the level will not have
            # dropped yet on the frame the lock is noticed. Give it a moment
            # instead of failing a feature that is working.
            if fraction is not None and fraction < 0.02:
                self.closed_after_lock = True
                self.phase = "done"
                self.finished = True
            elif time.monotonic() - self.locked_at > 2.0:
                self.closed_after_lock = False
                self.phase = "done"
                self.finished = True

    def on_key(self, app: App, ch: str) -> None:
        super().on_key(app, ch)
        if ch == "\x1b" and self.watcher is not None:
            self.watcher.stop()

    def render(self, app: App, width: int) -> list[str]:
        if not getattr(self, "watcher", None):
            return [verdict_line(None, "screen lock", "gdbus missing, cannot test")]
        if self.phase == "done":
            return [
                verdict_line(
                    bool(self.closed_after_lock),
                    "lock closed the microphone",
                    "the key is still held but the mic is shut"
                    if self.closed_after_lock
                    else "the microphone stayed open",
                )
            ]
        hint = {
            "need_open": "hold the PTT key",
            "holding": "now lock the session from another terminal",
            "locked": "lock seen, checking the level",
        }[self.phase]
        return [
            f"  {BOLD}{hint}{RESET}",
            f"  {level_line(app)}",
            f"  {DIM}in the other terminal run:  loginctl lock-session{RESET}",
            f"  {DIM}press nothing here. F12 is the panic key, not the lock key{RESET}",
        ]


class CheckCurves(Check):
    key = "8"
    title = "Ramp curves"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Ramp curves"
        self.finished = True
        self.rows = []
        original = app.cfg.audio.curve
        for curve in ("exp", "linear", "s_curve"):
            app.cfg.audio.curve = curve
            samples: list[tuple[float, float]] = []
            done = threading.Event()

            # bound explicitly, the loop rebinds done on the next pass
            def worker(done: threading.Event = done) -> None:
                app.mic.ramp(1.0, app.cfg.audio.attack_ms)
                time.sleep(0.25)
                app.mic.ramp(0.0, 40)
                done.set()

            threading.Thread(target=worker, daemon=True).start()
            start = time.monotonic()
            while not done.is_set():
                now = time.monotonic()
                samples.append((now - start, app.sample_volume() or 0.0))
                time.sleep(0.01)
            rise = next((t for t, v in samples if v >= 0.9), None)
            self.rows.append((curve, rise))
        app.cfg.audio.curve = original
        app.mic.force_silence()

    def render(self, app: App, width: int) -> list[str]:
        lines = []
        for curve, rise in self.rows:
            if rise is None:
                lines.append(verdict_line(None, curve, "never reached 90%"))
            else:
                lines.append(
                    verdict_line(
                        True,
                        curve,
                        f"reached 90% in {rise * 1000:.0f} ms, "
                        f"attack is {app.cfg.audio.attack_ms} ms",
                    )
                )
        lines.append("")
        lines.append(f"{DIM}Listen for a click on the rising edge. Esc returns.{RESET}")
        return lines


class CheckConfig(Check):
    key = "9"
    title = "Configuration in effect"

    def enter(self, app: App) -> None:
        super().enter(app)
        app.heading = "Configuration"
        self.finished = True
        self.data = app.cfg.to_dict()

    def render(self, app: App, width: int) -> list[str]:
        lines: list[str] = []
        for section, values in self.data.items():
            lines.append(f"{CYAN}{BOLD}{section}{RESET}")
            if isinstance(values, dict):
                for key, value in values.items():
                    if key == "enabled":
                        continue
                    lines.append(f"  {fit(key, 22)} {value}")
            else:
                lines.append(f"  {values}")
            lines.append("")
        return lines


# ------------------------------------------------------------------ the app


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.width, self.height = shutil.get_terminal_size((80, 24))
        self.heading = "Periferia"
        self.note = ""
        self.status = ""
        self.daemon = Daemon(cfg)
        self.mic = self.daemon.mic
        self.thread: threading.Thread | None = None
        self.logs = LogCapture()
        self.checks: list[Check] = [
            CheckEnvironment(),
            CheckDevices(),
            CheckAudio(),
            CheckProcessing(),
            CheckGate(),
            CheckPanic(),
            CheckLock(),
            CheckCurves(),
            CheckConfig(),
        ]
        self.current: Check | None = None
        self.results: dict[str, str] = {}
        self._last_volume: float | None = None
        self._volume_at = 0.0
        self._raw: Any = None

    # engine ---------------------------------------------------------------

    def start_engine(self) -> bool:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        self.logs.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(self.logs)
        root.setLevel(logging.INFO)

        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if self.mic.source:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return bool(self.mic.source)

    def stop_engine(self) -> None:
        self.daemon.stop(signal.SIGTERM, None)
        if self.thread is not None:
            self.thread.join(timeout=5)

    def sample_volume(self) -> float | None:
        """Current microphone volume, polled a few times a second.

        pactl is a subprocess and is far too slow to call every frame, so the
        value is cached. That is enough for a bar that a human reads.
        """
        now = time.monotonic()
        if now - self._volume_at < 0.1:
            return self._last_volume
        self._volume_at = now
        source = self.mic.source
        self._last_volume = volume_fraction(pipewire.get_source(source)) if source else None
        return self._last_volume

    # navigation -----------------------------------------------------------

    def leave_check(self) -> None:
        if self.current is not None:
            self.results[self.current.key] = self.current.title
        self.current = None
        self.heading = "Periferia"
        self.note = ""

    def enter_check(self, index: int) -> None:
        if not 0 <= index < len(self.checks):
            return
        check = self.checks[index]
        self.current = check
        check.enter(self)

    def run_all(self) -> None:
        for check in self.checks:
            self.enter_check(self.checks.index(check))
            self.current = None

    # rendering ------------------------------------------------------------

    def _ptt_label(self) -> str:
        code = hotkey.resolve_key(self.cfg.ptt.ptt_key)
        if code is None:
            return f"{self.cfg.ptt.ptt_key} (cannot resolve)"
        return f"{self.cfg.ptt.ptt_key}  ({hotkey.key_label(code)})"

    def _menu(self) -> list[str]:
        lines = [
            f"{BOLD}Periferia{CYAN} self check{RESET}",
            "",
            f"  PTT key      {self._ptt_label()}",
            f"  Panic key    {self.cfg.ptt.panic_key or 'off'}",
            f"  Microphone   {self.mic.source or DIM + 'not created' + RESET}",
            "",
        ]
        for check in self.checks:
            done = f" {GREEN}done{RESET}" if check.key in self.results else ""
            lines.append(f"  {BOLD}{check.key}{RESET}  {fit(check.title, 42)}{done}")
        lines += [
            "",
            f"  {BOLD}a{RESET}  run every check that needs no input",
            "",
            f"  {DIM}number opens a check, r repeats the current one, q quits{RESET}",
        ]
        return lines

    def frame(self) -> str:
        w = self.width
        body: list[str]
        if self.current is None:
            body = self._menu()
        else:
            body = [f"{BOLD}{self.current.title}{RESET}", ""]
            body += self.current.render(self, w - 4)
            if self.note:
                body += ["", f"  {YELLOW}{fit(self.note, w - 4)}{RESET}"]
        body += [
            "",
            f"{DIM}{'─' * w}{RESET}",
            f"{DIM}mic {RESET}{volume_bar(self.sample_volume(), 20)}",
        ]

        tail = self.logs.tail(max(0, self.height - len(body) - 6))
        for line in tail:
            body.append(f"{DIM}{fit(line, w)}{RESET}")

        return "\x1b[H\x1b[2J" + "\n".join(f"{fit(line, w)}{RESET}" for line in body)

    # loop -----------------------------------------------------------------

    def _read_key(self, timeout: float) -> str | None:
        if self._raw is None:
            return None
        try:
            readable, _, _ = select.select([self._raw], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not readable:
            return None
        return os.read(self._raw, 8).decode("utf-8", "ignore")

    def loop(self) -> int:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._raw = fd
        try:
            sys.stdout.write(HIDE + ALT)
            while True:
                sys.stdout.write(self.frame())
                sys.stdout.flush()
                ch = self._read_key(0.08)
                now = time.monotonic()
                if self.current is not None:
                    self.current.update(self, now)
                    # nothing may wait forever: a check that never finishes
                    # looks exactly like a check that is working
                    if (
                        not self.current.finished
                        and now - self.current.entered_at > self.current.timeout
                    ):
                        self.current.timed_out = True
                        self.current.finished = True
                if ch is None:
                    continue
                if ch in ("q", "\x03"):
                    return 0
                if self.current is not None:
                    if ch == "r":
                        self.current.enter(self)
                    else:
                        self.current.on_key(self, ch)
                    continue
                if ch.isdigit():
                    self.enter_check(int(ch) - 1)
                elif ch == "a":
                    for check in self.checks:
                        if isinstance(check, (CheckGate, CheckPanic, CheckLock)):
                            continue
                        self.enter_check(self.checks.index(check))
                        self.current = None
                    self.results["a"] = "auto"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(SHOW + ALT_OFF + FULL)
            sys.stdout.flush()


def run(cfg: Config | None = None) -> int:
    if not sys.stdin.isatty():
        print("the interface needs a terminal", file=sys.stderr)
        return 1
    cfg = cfg or config_mod.load()
    app = App(cfg)
    if not app.start_engine():
        print("the daemon would not start, run 'periferia check' first", file=sys.stderr)
        for line in app.logs.tail(8):
            print(f"  {line}", file=sys.stderr)
        return 1
    try:
        return app.loop()
    finally:
        app.stop_engine()
