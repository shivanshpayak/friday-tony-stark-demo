"""FRC mode and the spoken names of the WPILib apps."""
import sys

import pytest

from friday.tools import frc


def _project(root, name, mtime):
    path = root / name
    (path / ".wpilib").mkdir(parents=True)
    (path / ".wpilib" / "wpilib_preferences.json").write_text("{}")
    import os
    os.utime(path, (mtime, mtime))
    return path


def test_newest_robot_project_wins_and_non_projects_are_ignored(tmp_path):
    _project(tmp_path, "Gamma-2026", 1000)
    _project(tmp_path, "Gamma-2027", 3000)
    (tmp_path / "Notes").mkdir()
    assert frc.pick_robot_project("", tmp_path).name == "Gamma-2027"
    assert [p.name for p in frc.find_robot_projects(tmp_path)] == ["Gamma-2027", "Gamma-2026"]


def test_spoken_project_name_selects_it(tmp_path):
    _project(tmp_path, "Gamma-2026", 1000)
    _project(tmp_path, "Gamma-2027", 3000)
    assert frc.pick_robot_project("gamma 2026", tmp_path).name == "Gamma-2026"
    # An unknown name falls back to the newest rather than opening nothing.
    assert frc.pick_robot_project("omega", tmp_path).name == "Gamma-2027"


def test_no_projects_is_not_an_error(tmp_path):
    assert frc.pick_robot_project("", tmp_path) is None
    assert frc.pick_robot_project("", tmp_path / "missing") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Linux launcher")
class TestSpokenNames:
    """'WPILib' alone used to open Elastic (WPILib); 'FRC Visual Studio Code'
    fuzzy-matched plain Visual Studio Code."""

    @pytest.fixture(autouse=True)
    def apps(self, monkeypatch):
        from friday.tools import apps_linux
        entries = {
            "frc vs code": {"display": "FRC VS Code 2026", "exec": ["frccode2026"], "process": "frccode2026"},
            "elastic wpilib": {"display": "Elastic (WPILib) 2026", "exec": ["Elastic"], "process": "Elastic"},
            "advantagescope wpilib": {"display": "AdvantageScope (WPILib) 2026", "exec": ["AdvantageScope"], "process": "AdvantageScope"},
            "visual studio code": {"display": "Visual Studio Code", "exec": ["code"], "process": "code"},
        }
        monkeypatch.setattr(apps_linux, "_ensure_discovered", lambda: entries)
        self.resolve = lambda name: apps_linux._resolve(name)[0]

    @pytest.mark.parametrize("spoken", [
        "WPILib", "wpilib", "FRC VS Code 2026", "FRC VS Code",
        "FRC Visual Studio Code", "WPILib VS Code", "robot code",
    ])
    def test_frc_editor_names(self, spoken):
        assert self.resolve(spoken) == "FRC VS Code 2026"

    def test_plain_vs_code_is_still_plain_vs_code(self):
        assert self.resolve("Visual Studio Code") == "vscode"
        assert self.resolve("vs code") == "vscode"

    def test_dashboards(self):
        assert self.resolve("Elastic") == "Elastic (WPILib) 2026"
        assert self.resolve("advantage scope") == "AdvantageScope (WPILib) 2026"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
class TestSpokenNamesWindows:
    """The same spoken names on Windows, where the editor's Start Menu shortcut
    is "2026 WPILib VS Code". FRC mode asks for "frc vs code", which resolved to
    nothing there — so the Linux→Windows handoff opened everything but the
    editor."""

    @pytest.fixture(autouse=True)
    def apps(self, monkeypatch):
        from friday.tools import apps
        entries = {
            "wpilib vs code": {"lnk": "x.lnk", "display": "2026 WPILib VS Code", "process": None},
            "wpilib documentation": {"lnk": "x.lnk", "display": "2026 WPILib Documentation", "process": None},
            "elastic wpilib": {"lnk": "x.lnk", "display": "Elastic (WPILib) 2026", "process": None},
            "advantagescope wpilib": {"lnk": "x.lnk", "display": "AdvantageScope (WPILib) 2026", "process": None},
            "frc driver station": {"lnk": "x.lnk", "display": "FRC Driver Station", "process": None},
            "visual studio code": {"lnk": "x.lnk", "display": "Visual Studio Code", "process": None},
        }
        monkeypatch.setattr(apps, "_ensure_discovered", lambda: entries)
        self.resolve = lambda name: apps._resolve(name)[0]

    @pytest.mark.parametrize("spoken", [
        "frc vs code", "WPILib", "FRC VS Code", "FRC Visual Studio Code",
        "WPILib VS Code", "robot code",
    ])
    def test_frc_editor_names(self, spoken):
        assert self.resolve(spoken) == "2026 WPILib VS Code"

    def test_plain_vs_code_is_still_plain_vs_code(self):
        assert self.resolve("Visual Studio Code") == "vscode"
        assert self.resolve("vs code") == "vscode"

    def test_every_app_frc_mode_opens_resolves(self):
        assert self.resolve("frc driver station") == "FRC Driver Station"
        assert self.resolve("elastic") == "Elastic (WPILib) 2026"
        assert self.resolve("advantagescope") == "AdvantageScope (WPILib) 2026"
