"""Which keyboard profile belongs to the window that has focus.

Kept apart from the watcher that feeds it and from the remapper that applies
it, because this part is pure: profiles in, one name out. It is the only part
of per-window switching that can be tested without a compositor, and it is
where the mistakes that would show up as the wrong layout at the worst moment
would live.
"""

from __future__ import annotations

import dataclasses

from .config import ProfileConfig


@dataclasses.dataclass(slots=True)
class Window:
    """Just enough of a window to choose a profile by."""

    resource_class: str | None = None
    resource_name: str | None = None
    caption: str | None = None

    @classmethod
    def from_report(cls, report: object) -> Window:
        return cls(
            resource_class=getattr(report, "resource_class", None),
            resource_name=getattr(report, "resource_name", None),
            caption=getattr(report, "caption", None),
        )

    def has_identifier(self) -> bool:
        return bool(self.resource_class or self.resource_name)


def _same(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.strip().casefold() == right.strip().casefold()


def matches(profile: ProfileConfig, window: Window) -> bool:
    """Every criterion the profile states must hold. An empty one matches nothing.

    A profile that names no window is a fallback, not a match, and is handled
    separately. Treating it as a match would make it win over every real
    profile simply by being listed first.
    """
    if not profile.match:
        return False
    if profile.match.get("resource_class") and not _same(
        window.resource_class, profile.match["resource_class"]
    ):
        return False
    return not (
        profile.match.get("resource_name")
        and not _same(window.resource_name, profile.match["resource_name"])
    )


def fallback(profiles: list[ProfileConfig]) -> ProfileConfig | None:
    for profile in profiles:
        if not profile.match and profile.enabled:
            return profile
    return None


def select(profiles: list[ProfileConfig], window: Window) -> ProfileConfig | None:
    """The profile for this window, or the fallback if nothing claims it.

    Returns None rather than guessing when the window has no identifier at all,
    because matching on a window we cannot name is how a profile ends up
    applied to the wrong program.
    """
    if not window.has_identifier():
        return fallback(profiles)
    for profile in profiles:
        if profile.enabled and matches(profile, window):
            return profile
    return fallback(profiles)
