"""Tests for the terminal interface.

The interface itself cannot be driven here, because there is no terminal and no
microphone, so what is covered is the logic that decides pass or fail: reading
a volume out of pactl, drawing a bar, and the state machines behind the
interactive checks.
"""

from __future__ import annotations

from src.periferia import tui
from src.periferia.tui import Check, CheckGate, CheckPanic, volume_bar, volume_fraction


def test_volume_fraction_reads_the_first_channel() -> None:
    # pactl reports per channel and ours move together, so the first is enough
    source = {"volume": {"front-left": {"value": 32768}, "front-right": {"value": 32768}}}
    assert volume_fraction(source) == 0.5


def test_volume_fraction_handles_missing_and_broken_data() -> None:
    assert volume_fraction(None) is None
    assert volume_fraction({}) is None
    assert volume_fraction({"volume": {}}) is None
    assert volume_fraction({"volume": {"a": {}}}) is None
    assert volume_fraction({"volume": {"a": {"value": "loud"}}}) is None


def test_volume_fraction_is_clamped() -> None:
    assert volume_fraction({"volume": {"a": {"value": 999999}}}) == 1.0
    assert volume_fraction({"volume": {"a": {"value": -5}}}) == 0.0


def test_volume_bar_reflects_the_level() -> None:
    assert volume_bar(0.0, 10) == ".........."
    assert volume_bar(1.0, 10) == "##########"
    assert volume_bar(0.5, 10) == "#####....."
    # a missing source must be visible as unknown, not as silence
    assert volume_bar(None, 4) == "????"


def test_fit_never_overflows_the_width() -> None:
    assert tui.fit("short", 10) == "short"
    assert len(tui.fit("a much longer line of text", 10)) == 10
    assert tui.fit("abcdef", 3) == "ab…"
    assert tui.fit("abcdef", 1) == "a"


class _FakeDaemon:
    """Records the events the daemon would report, so a test can inject them."""

    def __init__(self) -> None:
        self.log: list[tuple[float, str]] = []
        self.clock = 0.0

    def add(self, name: str) -> None:
        self.clock += 1.0
        self.log.append((self.clock, name))

    def events(self, name: str, since: float = 0.0) -> list[float]:
        return [t for t, n in reversed(self.log) if n == name and t >= since]


class _FakeApp:
    """Stands in for App, with a volume the test controls."""

    def __init__(self, volume: float | None = None) -> None:
        self.volume = volume
        self.daemon = _FakeDaemon()
        self.cfg = _cfg()
        self.mic = _Mic()
        self.heading = ""
        self.note = ""
        self.left = 0

    def sample_volume(self) -> float | None:
        return self.volume

    def leave_check(self) -> None:
        self.left += 1


class _Mic:
    source = "echo-cancel-source"


class _PttCfg:
    ptt_key = "KEY_GRAVE"
    panic_key = "KEY_F12"
    device = "auto"


class _AudioCfg:
    target_volume = 1.0
    attack_ms = 10
    hold_ms = 200
    curve = "exp"


class _Cfg:
    ptt = _PttCfg()
    audio = _AudioCfg()


def _cfg() -> _Cfg:
    return _Cfg()


def test_gate_check_needs_a_quiet_start() -> None:
    # if the mic is already open, a press is not a press and the check is
    # meaningless, so it must wait for silence first
    app = _FakeApp(volume=0.8)
    check = CheckGate()
    check.enter(app)
    for _ in range(5):
        check.update(app, 0.0)
    assert check.phase == "idle"
    assert not check.finished


def test_gate_check_measures_a_full_cycle() -> None:
    app = _FakeApp(volume=0.0)
    check = CheckGate()
    check.enter(app)
    now = 0.0
    for _ in range(3):
        check.update(app, now)
        now += 0.1

    app.volume = 1.0
    for _ in range(3):  # key down
        check.update(app, now)
        now += 0.1
    assert check.phase == "holding"

    app.volume = 0.0
    for _ in range(3):  # key up
        check.update(app, now)
        now += 0.1
    assert check.phase == "closing"

    for _ in range(10):  # fade out observed
        check.update(app, now)
        now += 0.1
    assert check.finished

    lines = "\n".join(check.render(app, 80))
    assert "gate opened to the target level" in lines
    assert "peak 100% of 100% target" in lines
    assert "gate closed on release" in lines


def test_gate_check_fails_when_the_peak_is_low() -> None:
    # the key was seen but the microphone barely moved: that is a failure
    app = _FakeApp(volume=0.0)
    check = CheckGate()
    check.enter(app)
    now = 0.0
    for _ in range(3):
        check.update(app, now)
        now += 0.1
    app.volume = 0.2
    for _ in range(3):
        check.update(app, now)
        now += 0.1
    app.volume = 0.0
    for _ in range(20):
        check.update(app, now)
        now += 0.1
    assert check.finished
    lines = "\n".join(check.render(app, 80))
    assert "gate opened to the target level" in lines
    assert "peak 20% of 100% target" in lines


def _arm_panic(app: _FakeApp, check: CheckPanic) -> None:
    check.enter(app)
    check.update(app, 0.0)  # mic closed, nothing yet
    app.volume = 1.0
    check.update(app, 0.1)  # key held
    assert check.phase == "armed"


def test_panic_check_measures_the_drop_after_a_panic_event(monkeypatch) -> None:
    app = _FakeApp(volume=0.0)
    check = CheckPanic()
    _arm_panic(app, check)

    clock = [100.0]
    monkeypatch.setattr("src.periferia.tui.time.monotonic", lambda: clock[0])
    check.armed_at = 99.9
    app.daemon.log.append((99.95, "panic"))  # at or after armed_at
    check.update(app, 0.2)
    assert check.phase == "checking"
    assert check.saw_panic

    clock[0] = 100.02
    app.volume = 0.0
    check.update(app, 0.3)
    assert check.finished
    assert check.closed
    assert check.drop_ms is not None and check.drop_ms < 120

    lines = "\n".join(check.render(app, 80))
    assert "panic closed the microphone" in lines
    assert "hold_ms is 200" in lines


def test_panic_check_fails_when_the_mic_closes_without_a_panic(monkeypatch) -> None:
    # the key was simply released: the volume drops, but panic never fired, and
    # calling that a pass would be a lie
    app = _FakeApp(volume=0.0)
    check = CheckPanic()
    _arm_panic(app, check)

    clock = [100.0]
    monkeypatch.setattr("src.periferia.tui.time.monotonic", lambda: clock[0])
    check.armed_at = 99.0
    app.volume = 0.0  # released, no panic event anywhere

    check.update(app, 0.2)
    assert check.phase == "armed"  # still waiting, not a pass

    clock[0] = 120.0  # patience runs out
    check.update(app, 0.3)
    assert check.finished
    assert check.saw_panic is False

    lines = "\n".join(check.render(app, 80))
    assert "no panic event was seen" in lines
    assert "panic never fired" in lines


def test_panic_check_ignores_a_stale_event(monkeypatch) -> None:
    # a panic from before the hold started must not satisfy the check
    app = _FakeApp(volume=0.0)
    check = CheckPanic()
    app.daemon.add("panic")
    _arm_panic(app, check)

    monkeypatch.setattr("src.periferia.tui.time.monotonic", lambda: 100.0)
    check.update(app, 0.2)
    assert check.saw_panic is False
    assert check.phase == "armed"


def test_escape_leaves_a_check() -> None:
    app = _FakeApp()
    check: Check = CheckGate()
    check.on_key(app, "\x1b")
    assert app.left == 1


def test_enter_resets_a_previous_run() -> None:
    # pressing the number again must not keep the old verdict
    app = _FakeApp(volume=0.5)
    check = CheckGate()
    check.enter(app)
    check.phase = "done"
    check.finished = True
    check.enter(app)
    assert check.finished is False
    assert check.phase == "idle"
