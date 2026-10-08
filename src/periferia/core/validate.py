"""What is wrong with a config before the daemon obeys it.

A profile that never fires, or that fires and swallows the panic key, is
invisible until the moment someone needs it. This says so up front, where it
costs a command, instead of in the middle of a game.

Every check here is a mistake that produces silence rather than an error, which
is why they are worth making: a typo in a match criterion yields a profile that
simply never applies, and nothing in the running system objects.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from typing import Any

from . import kwinconfig, windowprofile

ERROR = "error"
WARNING = "warning"

# Matching on the caption would work right up until the window's contents change.
UNSTABLE_CRITERIA = ("caption",)

# Disabling these in one application can leave someone unable to get out of it:
# the window that would restore the keys is the one that no longer receives them.
RESCUE_KEYS = ("KEY_ESC", "KEY_TAB", "KEY_LEFTMETA", "KEY_RIGHTMETA", "KEY_SUPER")


@dataclasses.dataclass(frozen=True, slots=True)
class Problem:
    level: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclasses.dataclass(frozen=True, slots=True)
class Report:
    problems: tuple[Problem, ...] = ()

    @property
    def errors(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.level == ERROR)

    @property
    def warnings(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.level == WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors


def _criterion_fields() -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(windowprofile.Window))


def _key_values(pairs: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(pairs, dict):
        return pairs.items()
    return ()


def _is_disabled(target: Any) -> bool:
    return str(target).strip().lower() in ("none", "-", "off", "disable")


def check_profiles(
    profiles: Sequence[Any],
    reserved: Iterable[str] = (),
    present_codes: frozenset[int] | None = None,
) -> Report:
    """Every reason these profiles would not do what they look like they do."""
    found: list[Problem] = []
    known = _criterion_fields()
    reserved_codes = {str(code).strip().upper() for code in reserved if code}
    seen_names: dict[str, int] = {}
    previous: dict[tuple[tuple[str, Any], ...], str] = {}

    for index, profile in enumerate(profiles):
        name = getattr(profile, "name", f"profiles[{index}]")
        where = f"profile {name!r}"
        enabled = getattr(profile, "enabled", True)

        if name in seen_names:
            found.append(
                Problem(
                    ERROR,
                    where,
                    f"the same name is used twice, at positions"
                    f" {seen_names[name]} and {index}, so only one of them can be"
                    f" chosen by name",
                )
            )
        seen_names[name] = index

        match = getattr(profile, "match", None) or {}
        for key, wanted in _key_values(match):
            if key in UNSTABLE_CRITERIA:
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"{key} changes with the window's contents, so a profile"
                        f" matched on it would apply and stop applying while you"
                        f" look at the same program",
                    )
                )
            elif key not in known:
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"{key!r} is not a field a window has, so this profile can"
                        f" never fire. Known: {', '.join(sorted(known))}",
                    )
                )
            if isinstance(wanted, list) and not wanted:
                found.append(
                    Problem(ERROR, where, f"{key} is an empty list, which matches nothing")
                )

        criterion = tuple(sorted((str(k), str(v)) for k, v in _key_values(match)))
        if criterion:
            earlier = previous.get(criterion)
            if earlier is not None:
                found.append(
                    Problem(
                        WARNING,
                        where,
                        f"names exactly what {earlier!r} names, and comes after it,"
                        f" so it can never be chosen",
                    )
                )
            previous[criterion] = name
            if not enabled:
                found.append(
                    Problem(
                        WARNING,
                        where,
                        "names windows but is disabled, so it will never fire",
                    )
                )

        pairs = getattr(profile, "remap", None) or {}
        if criterion and enabled and not pairs:
            found.append(
                Problem(
                    WARNING,
                    where,
                    "will switch on this window but changes no keys, which does"
                    " nothing but cost a remap",
                )
            )

        for source, target in _key_values(pairs):
            source_key = str(source).strip().upper()
            if source_key in reserved_codes:
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"remaps {source}, which the user needs for PTT or panic,"
                        f" so they would lose the way out of a stuck gate",
                    )
                )
            if not _is_disabled(target) and str(target).strip().upper() in reserved_codes:
                found.append(
                    Problem(
                        WARNING,
                        where,
                        f"maps {source} onto {target}, which is reserved for PTT"
                        f" or panic",
                    )
                )
            if str(source).strip().upper() in RESCUE_KEYS and _is_disabled(target):
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"disables {source}. If this window then loses focus the"
                        f" key stays dead, and {source} is one of the ways out",
                    )
                )
            code = _code_of(source)
            if code is None:
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"{source} is not a key this project knows. It would be"
                        f" refused when the daemon tried to apply the profile",
                    )
                )
            # The target is checked here as well as in build_remap because a
            # misspelling of a word meaning "off" reads as a key that exists.
            # Left to the daemon it becomes a key that never works, with the
            # error in a log nobody is reading.
            if not _is_disabled(target) and _code_of(target) is None:
                found.append(
                    Problem(
                        ERROR,
                        where,
                        f"{source} maps to {target}, which is not a key this"
                        f" project knows. To turn a key off write none",
                    )
                )
            elif present_codes is not None and code not in present_codes:
                    found.append(
                        Problem(
                            WARNING,
                            where,
                            f"{source} is not on any keyboard this machine has,"
                            f" so it cannot be pressed",
                        )
                    )

    for index, profile in enumerate(profiles):
        speed = getattr(getattr(profile, "pointer", None), "speed", None)
        if speed is None:
            continue
        where = f"profile {getattr(profile, 'name', index)!r} pointer"
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            found.append(
                Problem(ERROR, where, f"speed is {speed!r}, which is not a number")
            )
        elif not kwinconfig.MIN_SPEED <= float(speed) <= kwinconfig.MAX_SPEED:
            found.append(
                Problem(
                    ERROR,
                    where,
                    f"speed {speed} is outside what a pointer can use; it has to be"
                    f" between {kwinconfig.MIN_SPEED} and {kwinconfig.MAX_SPEED},"
                    f" where {kwinconfig.NEUTRAL} is the speed the mouse was built with",
                )
            )
        elif not kwinconfig.available():
            found.append(
                Problem(
                    WARNING,
                    where,
                    "sets the pointer speed, which only KWin can apply here, and the"
                    " tools that write its configuration are not installed"
                    " (package kde-config-tools)",
                )
            )

    if profiles and all(not (getattr(p, "match", None) or {}) for p in profiles):
        found.append(
            Problem(
                WARNING,
                "profiles",
                "no profile names a window, so every window will fall through to"
                " the first one and the rest are never used",
            )
        )
    elif profiles and not any(
        not (getattr(p, "match", None) or {}) and getattr(p, "enabled", True)
        for p in profiles
    ):
        found.append(
            Problem(
                WARNING,
                "profiles",
                "no profile will catch every other window, so anything unmatched"
                " gets no remap at all",
            )
        )
    if not profiles:
        found.append(Problem(WARNING, "profiles", "there are no profiles at all"))

    return Report(tuple(found))


def _code_of(name: str) -> int | None:
    try:
        from ..modules.hotkey import resolve_key

        return resolve_key(str(name).strip().upper())
    except Exception:
        return None


def check_devices(devices: Sequence[Any]) -> Report:
    """Reasons a device entry would never apply, or silently do nothing.

    The top of `devices` is a match table: the first entry whose rules all
    agree wins, and an entry with no rules agrees with any device. The traps
    are therefore positional. An entry that sits after a match-anything entry
    can never win, and an entry that wins but overrides no section changes
    nothing, so the device it was written for gets the default audio settings
    and nobody hears the difference until the hardware does.
    """
    found: list[Problem] = []
    seen_names: dict[str, int] = {}
    reached = True

    for index, device in enumerate(devices):
        name = getattr(device, "name", "") or f"devices[{index}]"
        where = f"device {name!r}"
        enabled = getattr(device, "enabled", True)
        match = getattr(device, "match", None) or {}

        if name in seen_names:
            found.append(
                Problem(
                    ERROR,
                    where,
                    f"the same name is used twice, at positions"
                    f" {seen_names[name]} and {index}, so which one is meant"
                    " is a guess",
                )
            )
        seen_names[name] = index

        if not enabled:
            continue

        if not reached:
            found.append(
                Problem(
                    WARNING,
                    where,
                    "matches nothing an earlier entry would not, and sits after"
                    " an entry that matches any device, so it can never apply",
                )
            )
            continue

        if not match:
            found.append(
                Problem(
                    WARNING,
                    where,
                    "matches any device, so every entry after it can never"
                    " apply; a fallback entry earns that name best when it"
                    " sits last",
                )
            )
            reached = False
        elif not getattr(device, "audio", None) and not getattr(device, "processing", None):
            found.append(
                Problem(
                    WARNING,
                    where,
                    "matches devices but overrides nothing: with neither audio"
                    " nor processing keys it changes no setting at all",
                )
            )

    if not devices:
        found.append(Problem(WARNING, "devices", "there are no device entries at all"))

    return Report(tuple(found))


def check_macros(
    macros: Sequence[Any],
    profiles: Sequence[Any] = (),
    reserved: Iterable[str] = (),
) -> Report:
    """What is wrong with the macros, and with the ones profiles redefine.

    Macros type into whatever is in front of the user, so the checks lean towards
    refusing rather than warning. The one that matters most is a macro replaying
    its own trigger: the key is bound to the macro, the macro presses the key,
    and the key is still bound to the macro.
    """
    from . import macro as macro_mod

    found: list[Problem] = []
    reserved_names = {str(name).strip().upper() for name in reserved if str(name).strip()}
    reserved_codes = {code for code in (_code_of(n) for n in reserved_names) if code is not None}

    # Scopes are walked separately. Merging them into one list made a profile
    # override look exactly like a duplicate name and a rebound key look like a
    # clash, so the feature could not be described without the report objecting
    # to it. Within a scope a repeated name is a mistake; across scopes the same
    # name is the override mechanism working.
    scopes: list[tuple[str, list[Any]]] = [("macros", list(macros))]
    for profile in profiles:
        if getattr(profile, "enabled", True):
            where = f"profile '{getattr(profile, 'name', '?')}'"
            scopes.append((where, list(getattr(profile, "macros", None) or [])))
            break

    # Only the first enabled profile decides what a key plays, so only it can
    # produce a real clash. A later profile is reported separately if it needs
    # it, but two profiles that both want KEY_F5 is not a conflict today.
    effective_names: dict[str, tuple[str, Any]] = {}
    for scope, entries in scopes:
        seen_names: dict[str, int] = {}

        for index, entry in enumerate(entries):
            name = getattr(entry, "name", "") or ""
            steps = getattr(entry, "steps", None) or []

            if not getattr(entry, "enabled", True):
                continue

            if not name:
                found.append(
                    Problem(
                        ERROR,
                        f"{scope} entry {index + 1}",
                        "it has no name, so nothing can refer to it",
                    )
                )
                name = f"macro {index + 1}"

            if not steps:
                found.append(
                    Problem(
                        WARNING,
                        f"macro '{name}'",
                        "it has no keypresses, so there is nothing to play",
                    )
                )
                continue

            if name in seen_names:
                found.append(
                    Problem(
                        ERROR,
                        f"macro '{name}'",
                        f"the same name is used twice in {scope}, at positions"
                        f" {seen_names[name]} and {index + 1}, so which one is meant"
                        " is a guess",
                    )
                )
            seen_names[name] = index + 1

            try:
                built = macro_mod.from_config(entry, _code_of)
            except macro_mod.MacroError as exc:
                found.append(
                    Problem(
                        ERROR,
                        f"macro '{name}'",
                        f"{exc}. It would be refused when the macro played",
                    )
                )
                continue

            effective_names[name] = (scope, entry)
            bind = (getattr(entry, "bind", "") or "").strip().upper()
            if not bind:
                found.append(
                    Problem(
                        WARNING,
                        f"macro '{name}'",
                        "it is not on a key, so nothing can play it",
                    )
                )
            elif _code_of(bind) is None:
                found.append(
                    Problem(ERROR, f"macro '{name}'", f"'{bind}' is not a key this project knows")
                )
            elif bind in reserved_names:
                found.append(
                    Problem(
                        ERROR,
                        f"macro '{name}'",
                        f"it is bound to {bind}, which is a key this program listens for."
                        " A macro on that key would fire while the other one works",
                    )
                )

            for step in built.steps:
                if step.code in reserved_codes:
                    found.append(
                        Problem(
                            ERROR,
                            f"macro '{name}'",
                            "it plays a key this program listens for, so it would"
                            " interfere with it every time it played",
                        )
                    )
                    break

            try:
                macro_mod.plan(built, _code_of(bind) if bind else None, reserved_codes)
            except macro_mod.MacroError as exc:
                found.append(Problem(ERROR, f"macro '{name}'", str(exc)))

    # A key can end up with two macros after the overrides are applied: a global
    # macro on KEY_F5 and a profile macro that moves a different name onto it.
    # That only matters for the profile that is actually chosen, so it is checked
    # on the resolved set rather than on the file.
    resolved: dict[str, str] = {}
    for name, (_scope, entry) in effective_names.items():
        bind = (getattr(entry, "bind", "") or "").strip().upper()
        if not bind or _code_of(bind) is None or bind in reserved_names:
            continue
        if bind in resolved:
            found.append(
                Problem(
                    ERROR,
                    f"macro '{name}'",
                    f"after profiles are applied it shares {bind} with '{resolved[bind]}',"
                    " so only one of them will play",
                )
            )
        else:
            resolved[bind] = name

    return Report(tuple(found))
