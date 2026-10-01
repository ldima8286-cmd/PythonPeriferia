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

from . import windowprofile

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
            elif present_codes is not None and code not in present_codes:
                    found.append(
                        Problem(
                            WARNING,
                            where,
                            f"{source} is not on any keyboard this machine has,"
                            f" so it cannot be pressed",
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
