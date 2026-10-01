"""Macro logic with no device in it.

Everything here is a function of the config and of what was recorded, which is
what makes a macro system testable at all. The parts that touch a keyboard live
in modules/ and are told what to emit rather than deciding for themselves.

Two decisions are baked in rather than left to callers, because both of them
turn a recorded macro into something nobody can stop:

- A macro never plays its own bind key. Otherwise a macro bound to F5 that
  contains F5 replays itself forever, one press at a time, and the only way out
  is killing the daemon.
- Gaps and holds are clamped. A recording that sat idle for an hour over lunch
  would otherwise replay as a one hour wait, and a hold with no release makes
  everything typed after it come out as one held modifier.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Sequence
from typing import Any

MAX_GAP_MS = 5_000
MAX_HOLD_MS = 2_000
DEFAULT_HOLD_MS = 40


class MacroError(Exception):
    """A macro that cannot be played, named so the message can be."""


@dataclasses.dataclass(frozen=True, slots=True)
class Step:
    """One keypress, timed. Codes are resolved to numbers only at the edge."""

    code: int
    gap_ms: int
    hold_ms: int


def clamp_gap(ms: int) -> int:
    return max(0, min(int(ms), MAX_GAP_MS))


def clamp_hold(ms: int) -> int:
    return max(1, min(int(ms), MAX_HOLD_MS))


@dataclasses.dataclass(frozen=True, slots=True)
class Macro:
    """A named, replayable sequence of keypresses."""

    name: str
    steps: tuple[Step, ...]
    bind: str = ""
    enabled: bool = True

    @property
    def duration_ms(self) -> int:
        """How long a full playback takes, used to answer 'is this sane'."""
        return sum(step.gap_ms + step.hold_ms for step in self.steps)

    def playable(self) -> str | None:
        """Why this macro cannot be played, or None when it can."""
        if not self.steps:
            return "it has no keypresses in it"
        total = self.duration_ms
        if total > 60_000:
            return f"it takes {total // 1000} seconds to play, which is not a keypress"
        return None


def from_config(entry: Any, resolve: Any) -> Macro:
    """Build a Macro from a MacroConfig, resolving key names to codes.

    `resolve` maps a name like KEY_F5 to its code, and returns None for a name it
    does not know. Unknown names are an error here rather than skipped, because
    a silently dropped step produces a macro that types the wrong thing.
    """
    steps: list[Step] = []
    for raw in getattr(entry, "steps", None) or []:
        name = getattr(raw, "key", "")
        code = resolve(name)
        if code is None:
            raise MacroError(f"'{name}' is not a key this project knows")
        steps.append(
            Step(
                code=code,
                gap_ms=clamp_gap(getattr(raw, "gap_ms", 0)),
                hold_ms=clamp_hold(getattr(raw, "hold_ms", DEFAULT_HOLD_MS)),
            )
        )
    return Macro(
        name=getattr(entry, "name", "") or "",
        steps=tuple(steps),
        bind=getattr(entry, "bind", "") or "",
        enabled=getattr(entry, "enabled", True),
    )


def effective(
    globals_: Iterable[Any], profiles: Sequence[Any], resolve: Any
) -> list[Macro]:
    """Global macros, with the active profile's versions of them layered on top.

    A profile replaces a macro by name rather than appending to it, so a window
    that binds 'hello' to a different key does not end up with two macros
    fighting over one key. Profiles after the first are ignored, which is the
    same rule the remap table uses and the reason only one is meant to match.
    """
    chosen: dict[str, Macro] = {}
    order: list[str] = []
    for entry in globals_:
        macro = from_config(entry, resolve)
        if macro.name not in chosen:
            order.append(macro.name)
        chosen[macro.name] = macro

    for profile in profiles:
        if not getattr(profile, "enabled", True):
            continue
        for entry in getattr(profile, "macros", None) or []:
            macro = from_config(entry, resolve)
            if macro.name not in chosen:
                order.append(macro.name)
            chosen[macro.name] = macro
        break  # only the first enabled profile applies, as with remap

    return [chosen[name] for name in order if chosen[name].enabled]


def bindings(macros: Iterable[Macro], resolve: Any) -> dict[int, list[Macro]]:
    """Map a trigger key code to the macros it plays.

    A list rather than one macro per key, because two macros sharing a key is a
    mistake the caller must see, not one to resolve silently in the middle of a
    keypress.
    """
    out: dict[int, list[Macro]] = {}
    for macro in macros:
        if not macro.bind:
            continue
        code = resolve(macro.bind)
        if code is None:
            continue
        out.setdefault(code, []).append(macro)
    return out


def playback_steps(
    macro: Macro, trigger_code: int | None, reserved: Iterable[int] = ()
) -> tuple[Step, ...]:
    """The steps to actually send, with the ones that must not be sent removed.

    Dropped, not reordered: a macro bound to F5 that recorded F5 would otherwise
    fire itself on every pass. Reserved codes are dropped for the same reason a
    remap refuses to touch them.
    """
    banned = set(reserved)
    if trigger_code is not None:
        banned.add(trigger_code)
    return tuple(step for step in macro.steps if step.code not in banned)


def plan(
    macro: Macro, trigger_code: int | None, reserved: Iterable[int] = ()
) -> list[Step]:
    """Validate a macro and return what playback will send.

    Raises rather than returning a partial list, so a caller cannot start playing
    half a macro and think it worked.
    """
    reason = macro.playable()
    if reason is not None:
        raise MacroError(f"macro '{macro.name}' cannot be played because {reason}")
    steps = playback_steps(macro, trigger_code, reserved)
    if not steps:
        raise MacroError(
            f"macro '{macro.name}' would only press its own key, so it was never recorded"
        )
    return list(steps)


def describe(macro: Macro, label: Callable[[int], str] | None = None) -> str:
    """One line a person can read to check a macro is the one they meant.

    Codes alone are unreadable: "35 23" tells nobody that the macro types h and
    i, which is the only reason anyone looks at this line. `label` is what turns
    a code into KEY_H; without it the numbers are shown rather than guessed at.
    """
    name_of = label if label is not None else str
    keys = " ".join(name_of(step.code) for step in macro.steps[:6])
    if len(macro.steps) > 6:
        keys += f" +{len(macro.steps) - 6} more"
    seconds = macro.duration_ms / 1000
    count = f"{len(macro.steps)} key" if len(macro.steps) == 1 else f"{len(macro.steps)} keys"
    return f"{count}, {seconds:.1f}s, [{keys}]"


@dataclasses.dataclass(slots=True)
class Recorder:
    """Turns a stream of key events into timed steps.

    Fed (monotonic seconds, code, value) triples and knows nothing about
    evdev, so the rules that matter can be tested without a keyboard.

    Two of those rules are not obvious:

    - Auto-repeat is dropped. A held key reports value 2 over and over, and
      recording those would turn one long press into a burst of dozens.
    - The key that started the recording is never recorded, because it is the
      key that has to stop it. Recording it makes the stop key part of the
      macro, and the macro then types its own trigger on replay.
    """

    ignore: frozenset[int] = frozenset()
    steps: list[Step] = dataclasses.field(default_factory=list)
    _pending: int | None = None
    _pending_gap: int = 0
    _pressed_at: float = 0.0
    _released_at: float | None = None

    def feed(self, at: float, code: int, value: int) -> None:
        if code in self.ignore:
            return
        if value == 2:  # auto-repeat, not a new press
            return
        if value == 1:
            gap = 0 if self._released_at is None else round((at - self._released_at) * 1000)
            self._pending = code
            self._pending_gap = clamp_gap(gap)
            self._pressed_at = at
            return
        if value != 0:
            return
        if self._pending != code:
            return  # a release with no press: a key that was already down
        held = round((at - self._pressed_at) * 1000)
        self.steps.append(Step(code=code, gap_ms=self._pending_gap, hold_ms=clamp_hold(held)))
        self._pending = None
        self._released_at = at

    def finish(self) -> tuple[Step, ...]:
        """End the recording, returning what was captured.

        A key still held when recording stops is dropped rather than given a
        zero-length hold, because the release never came and there is no honest
        duration to record for it.
        """
        self._pending = None
        return tuple(self.steps)


