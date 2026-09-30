"""Which profile belongs to which window. Pure, so it can be tested without KWin."""

from __future__ import annotations

import pytest

from periferia.core import windowprofile
from periferia.core.config import ProfileConfig


def _profile(name: str, **match: str) -> ProfileConfig:
    return ProfileConfig(name=name, match=match)


@pytest.fixture()
def profiles() -> list[ProfileConfig]:
    return [
        _profile("game", resource_class="steam"),
        _profile("chat", resource_class="discord", resource_name="discord"),
        ProfileConfig(name="default"),
    ]


class TestSelect:
    def test_a_matching_profile_wins(self, profiles: list[ProfileConfig]) -> None:
        window = windowprofile.Window(resource_class="steam")
        assert windowprofile.select(profiles, window).name == "game"

    def test_criteria_are_combined(self, profiles: list[ProfileConfig]) -> None:
        half = windowprofile.Window(resource_class="discord")
        assert windowprofile.select(profiles, half).name == "default"
        both = windowprofile.Window(resource_class="discord", resource_name="discord")
        assert windowprofile.select(profiles, both).name == "chat"

    def test_unclaimed_windows_get_the_fallback(
        self, profiles: list[ProfileConfig]
    ) -> None:
        window = windowprofile.Window(resource_class="firefox")
        assert windowprofile.select(profiles, window).name == "default"

    def test_naming_lost_goes_to_the_fallback(
        self, profiles: list[ProfileConfig]
    ) -> None:
        assert windowprofile.select(profiles, windowprofile.Window()).name == "default"

    def test_a_window_with_only_a_caption_is_not_matched(
        self, profiles: list[ProfileConfig]
    ) -> None:
        window = windowprofile.Window(caption="steam — games")
        assert windowprofile.select(profiles, window).name == "default"


class TestCase:
    def test_compositor_case_does_not_matter(
        self, profiles: list[ProfileConfig]
    ) -> None:
        window = windowprofile.Window(resource_class="Steam", resource_name="STEAM")
        assert windowprofile.select(profiles, window).name == "game"

    def test_surrounding_space_does_not_matter(self) -> None:
        one = [_profile("game", resource_class="steam")]
        window = windowprofile.Window(resource_class="  steam  ")
        assert windowprofile.select(one, window).name == "game"


class TestFallbackIsNotAWildcard:
    def test_a_profile_without_match_matches_nothing(self) -> None:
        bare = ProfileConfig(name="default")
        assert not windowprofile.matches(bare, windowprofile.Window(resource_class="steam"))

    def test_listed_first_it_still_does_not_win(
        self, profiles: list[ProfileConfig]
    ) -> None:
        first_is_fallback = [profiles[2], *profiles[:2]]
        window = windowprofile.Window(resource_class="steam")
        assert windowprofile.select(first_is_fallback, window).name == "game"


class TestDisabled:
    def test_a_disabled_profile_is_not_selected(
        self, profiles: list[ProfileConfig]
    ) -> None:
        profiles[0].enabled = False
        window = windowprofile.Window(resource_class="steam")
        assert windowprofile.select(profiles, window).name == "default"

    def test_a_disabled_fallback_is_not_offered(
        self, profiles: list[ProfileConfig]
    ) -> None:
        profiles[2].enabled = False
        window = windowprofile.Window(resource_class="firefox")
        assert windowprofile.select(profiles, window) is None


class TestOrder:
    def test_the_first_matching_profile_wins(self) -> None:
        two = [
            _profile("first", resource_class="steam"),
            _profile("second", resource_class="steam"),
        ]
        window = windowprofile.Window(resource_class="steam")
        assert windowprofile.select(two, window).name == "first"


class TestFromReport:
    def test_it_reads_what_the_probe_found(self) -> None:
        class Report:
            resource_class = "org.kde.konsole"
            resource_name = "konsole"
            caption = "bash"

        window = windowprofile.Window.from_report(Report())
        assert window.resource_class == "org.kde.konsole"
        assert window.has_identifier()

    def test_a_report_with_nothing_in_it(self) -> None:
        assert not windowprofile.Window.from_report(object()).has_identifier()
