"""close_app must never take down the Windows shell or JARVIS itself.

Bug (2026-09-22, live): "close File Explorer" resolved to the whitelist entry
{"process": "explorer.exe"} and ran `taskkill /F /IM explorer.exe`. explorer.exe
also IS the Windows shell — the taskbar, Start menu and Alt+Tab switcher died with
the folder window. File Explorer must be closed by closing its windows
(class CabinetWClass), never by killing the process. Same class of bug: a Start
Menu "Python" shortcut resolves to python.exe, which is what JARVIS runs on.
"""
import pytest

from friday.tools import apps


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *a, **k):
        self.calls.append(cmd)

        class R:
            returncode = 0
            stdout = stderr = ""
        return R()


@pytest.fixture
def run(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(apps.subprocess, "run", rec)
    return rec


def _killed(run) -> list:
    return [c for c in run.calls if c and c[0] == "taskkill"]


def test_closing_file_explorer_closes_windows_not_the_shell(monkeypatch, run):
    monkeypatch.setattr(apps, "_close_explorer_windows", lambda: 2)
    result = apps.close_app("File Explorer")
    assert _killed(run) == [], "must never taskkill explorer.exe — it is the shell"
    assert result == "Closed File Explorer."


def test_file_explorer_not_open(monkeypatch, run):
    monkeypatch.setattr(apps, "_close_explorer_windows", lambda: 0)
    assert apps.close_app("file explorer") == "File Explorer wasn't open."
    assert _killed(run) == []


def test_discovered_shortcut_to_explorer_is_also_protected(monkeypatch, run):
    """The Start Menu 'File Explorer' .lnk resolves to EXPLORER.EXE too."""
    monkeypatch.setattr(apps, "_resolve", lambda name: (
        "File Explorer", {"lnk": "x.lnk", "display": "File Explorer",
                          "process": "EXPLORER.EXE"}, "discovered"))
    monkeypatch.setattr(apps, "_close_explorer_windows", lambda: 1)
    assert apps.close_app("explorer thing") == "Closed File Explorer."
    assert _killed(run) == []


@pytest.mark.parametrize("process", ["python.exe", "pythonw.exe", "Python.EXE"])
def test_refuses_to_kill_the_process_jarvis_runs_on(monkeypatch, run, process):
    monkeypatch.setattr(apps, "_resolve", lambda name: (
        "Python 3.11", {"lnk": "p.lnk", "display": "Python 3.11",
                        "process": process}, "discovered"))
    result = apps.close_app("python")
    assert _killed(run) == []
    assert "can't close" in result.lower()


def test_ordinary_apps_still_close_by_process(monkeypatch, run):
    monkeypatch.setattr(apps, "_resolve", lambda name: (
        "spotify", {"launch": "spotify", "process": "Spotify.exe"}, "pinned"))
    assert apps.close_app("spotify") == "Closed spotify."
    assert _killed(run) == [["taskkill", "/F", "/IM", "Spotify.exe"]]


def test_window_closer_only_touches_file_explorer_windows():
    closed = []
    windows = [(1, "CabinetWClass"), (2, "Shell_TrayWnd"), (3, "CabinetWClass"),
               (4, "Chrome_WidgetWin_1"), (5, "Progman")]
    n = apps._close_explorer_windows(windows=windows, post_close=closed.append)
    assert n == 2
    assert closed == [1, 3], "taskbar (Shell_TrayWnd) / desktop (Progman) untouched"


def test_window_enumeration_sees_the_real_taskbar():
    """Read-only check that the ctypes enumeration works on this machine."""
    classes = {cls for _, cls in apps._top_level_windows()}
    assert "Shell_TrayWnd" in classes


def test_powershell_fallback_can_never_match_protected_processes(monkeypatch, run):
    """The fallback (apps with no known exe) matches WINDOW TITLES too: a folder
    named "WhatsApp" open in Explorer made explorer.exe's title match "close
    WhatsApp". The protected-process exclusion must filter before any matching."""
    monkeypatch.setattr(apps, "_resolve", lambda name: (
        "WhatsApp", {"app_id": "WhatsApp!App", "display": "WhatsApp",
                     "process": None}, "discovered"))
    apps.close_app("whatsapp")
    ps = [c for c in run.calls if c and c[0] == "powershell"]
    assert ps, "expected the PowerShell fallback to run"
    script = ps[0][-1]
    # Pipeline stages are " | "-separated; a bare "|" is regex alternation.
    stages = [s.strip() for s in script.split(" | ")]
    exclusion = next(i for i, s in enumerate(stages) if "-notmatch" in s)
    match = next(i for i, s in enumerate(stages) if "MainWindowTitle" in s)
    assert exclusion < match, "exclusion must run before title/name matching"
    for proc in ("explorer", "python", "pythonw"):
        assert proc in stages[exclusion]
