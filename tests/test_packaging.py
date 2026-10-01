"""The packaging files, checked without packaging anything.

An AppImage is built on someone else's machine, weeks later, and a broken
desktop entry or a spec pointing at a file that no longer exists is found at the
worst possible moment. None of that needs a build to catch: every path these
files name can be checked against the tree right now.

desktop-file-validate is not available in this environment, so the required
keys and the entry point's existence are checked here instead.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DESKTOP = PACKAGING / "periferia.desktop"
APPDATA = PACKAGING / "periferia.appdata.xml"
ICON = PACKAGING / "icon" / "periferia.svg"
APPRUN = PACKAGING / "AppRun"
SPEC = PACKAGING / "periferia.spec"
BUILD = ROOT / "scripts" / "build-appimage.sh"


def _desktop() -> dict[str, str]:
    """Parse the desktop entry without inheriting the file format's opinions.

    Values are split on the first '=' so a '=' inside a translated Comment
    survives, which is exactly what happens with real translations.
    """
    entries: dict[str, str] = {}
    for raw in DESKTOP.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        entries[key.strip()] = value.strip()
    return entries


class TestTheDesktopEntry:
    def test_it_says_what_it_is(self) -> None:
        entries = _desktop()
        assert entries["Type"] == "Application"
        assert entries["Name"]
        assert entries["Comment"]
        assert "Terminal" in entries

    def test_it_is_localised_into_russian(self) -> None:
        raw = DESKTOP.read_text(encoding="utf-8")
        assert "Name[ru]=" in raw
        assert "Comment[ru]=" in raw

    def test_it_runs_a_command_that_exists(self) -> None:
        """The single most expensive mistake here is a name that looks right.

        A desktop file naming an entry point that was renamed produces an icon
        in the launcher that silently does nothing when clicked. The name is
        therefore resolved against the real entry points rather than trusted.
        """
        command = _desktop()["Exec"].split()[0]
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert f"{command} = " in pyproject, f"{command} is not a declared entry point"

    def test_its_icon_exists_under_the_name_it_asks_for(self) -> None:
        icon = _desktop()["Icon"]
        found = list(PACKAGING.glob(f"icon/{icon}.*"))
        assert found, f"no icon named {icon} in packaging/icon"

    def test_it_is_filed_under_something_reasonable(self) -> None:
        categories = set(_desktop()["Categories"].split(";"))
        assert "AudioVideo" in categories
        assert "Audio" in categories


class TestTheIcon:
    def test_it_parses_as_svg(self) -> None:
        root = ET.parse(ICON).getroot()
        assert root.tag.endswith("svg")

    def test_it_says_its_own_size_so_scalers_do_not_guess(self) -> None:
        root = ET.parse(ICON).getroot()
        assert root.get("viewBox"), "a viewBox is what lets the icon scale"
        assert root.get("width") == root.get("height")


class TestTheAppRun:
    def test_it_is_executable(self) -> None:
        assert os.access(APPRUN, os.X_OK), "AppImage will not run a file that is not executable"

    def test_it_execs_the_binary_that_was_frozen(self) -> None:
        """exec, not a bare call.

        Without exec the AppRun stays alive as a parent, so closing the window
        leaves a process behind holding the microphone's state file.
        """
        text = APPRUN.read_text(encoding="utf-8")
        assert "exec " in text
        assert "usr/bin/periferia-gui" in text

    def test_it_has_a_shebang(self) -> None:
        assert APPRUN.read_text(encoding="utf-8").startswith("#!")


class TestTheSpec:
    def test_it_freezes_a_file_that_exists(self) -> None:
        """This caught a real one.

        The spec first named src/periferia/gui/__main__.py, which was never
        written. PyInstaller would have failed at build time on the machine
        trying to package, rather than here.
        """
        text = SPEC.read_text(encoding="utf-8")
        start = text.index("[", text.index("Analysis("))
        entry = text[start + 1 : text.index("]", start)].strip().strip("\"'")
        assert (PACKAGING / entry).exists(), f"spec freezes {entry}, which does not exist"

    def test_it_does_not_freeze_the_console_tools(self) -> None:
        """The image is a GUI launcher.

        A frozen daemon that only works from a terminal nobody can reach is dead
        weight of tens of megabytes, since it drags in the PipeWire client
        libraries.
        """
        text = SPEC.read_text(encoding="utf-8")
        assert "console=False" in text
        assert "gui.window" not in text


class TestTheBuildScript:
    def test_it_is_executable(self) -> None:
        assert os.access(BUILD, os.X_OK)

    def test_it_fails_rather_than_packaging_something_that_will_not_start(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text
        assert "failed to start" in text, "a frozen build must be started before it is packaged"

    def test_it_installs_the_files_the_appdir_layout_requires(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        for needed in ("periferia.desktop", "AppRun", "periferia.svg"):
            assert needed in text, f"{needed} is never installed into the AppDir"

    def test_it_refuses_to_build_from_a_dirty_tree(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        assert "Uncommitted changes" in text

    def test_it_names_the_things_it_needs_from_the_host(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        for tool in ("python3", "curl", "squashfuse"):
            assert f'need {tool} ' in text, f"the script uses {tool} without checking for it"


class TestTheAppData:
    def test_it_parses(self) -> None:
        assert ET.parse(APPDATA).getroot().tag == "component"

    def test_its_id_matches_the_desktop_file_it_describes(self) -> None:
        """A mismatch here is what makes software centres reject the upload."""
        root = ET.parse(APPDATA).getroot()
        identifier = root.findtext("id")
        assert identifier == DESKTOP.name, (
            f"appdata id {identifier!r} does not match {DESKTOP.name!r},"
            " and software centres reject that"
        )

    def test_it_names_a_launchable_desktop_file(self) -> None:
        root = ET.parse(APPDATA).getroot()
        launchable = root.find("launchable")
        assert launchable is not None
        assert launchable.get("type") == "desktop-id"

    def test_it_references_bins_that_the_project_actually_builds(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        root = ET.parse(APPDATA).getroot()
        binaries = [b.text for b in root.findall("provides/binary")]
        assert binaries
        for binary in binaries:
            assert f"{binary} = " in pyproject


@pytest.mark.parametrize("name", ["periferia.desktop", "periferia.spec", "launcher.py"])
def test_the_frozen_entry_point_still_imports(name: str) -> None:
    """The launcher is what runs on a user's machine and in no test.

    It is frozen rather than imported, so a typo in it would otherwise sit
    unnoticed until the first packaged build.
    """
    import importlib.util

    path = PACKAGING / name
    if path.suffix != ".py":
        return
    spec = importlib.util.spec_from_file_location("periferia_launcher_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module._run)
