"""Linux app launcher: .desktop discovery, and the same guarantee the Windows
close path gives — close_app never takes down the desktop shell or JARVIS."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux launcher")

from friday.tools import apps_linux as apps  # noqa: E402


def _desktop(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("[Desktop Entry]\nType=Application\n" + body, encoding="utf-8")
    return path


def test_exec_field_codes_are_dropped(tmp_path):
    entry = apps._parse_desktop_file(
        _desktop(tmp_path, "s.desktop", "Name=Spotify\nExec=/snap/bin/spotify %U\n"))
    assert entry["exec"] == ["/snap/bin/spotify"]
    assert entry["process"] == "spotify"


def test_wrapper_commands_do_not_become_the_process_name(tmp_path):
    entry = apps._parse_desktop_file(_desktop(
        tmp_path, "f.desktop",
        "Name=Thing\nExec=env FOO=1 /usr/bin/flatpak run --branch=stable org.x.Thing @@u %U @@\n"))
    assert entry["process"] == "org.x.Thing"


@pytest.mark.parametrize("body", [
    "Name=Hidden\nExec=x\nNoDisplay=true\n",
    "Name=Gone\nExec=x\nHidden=true\n",
    "Name=NoExec\n",
])
def test_unlaunchable_entries_are_skipped(tmp_path, body):
    assert apps._parse_desktop_file(_desktop(tmp_path, "x.desktop", body)) is None


@pytest.fixture
def no_real_processes(monkeypatch):
    """Capture which process names close_app goes looking for; kill nothing."""
    asked = []
    monkeypatch.setattr(apps, "_matching_processes",
                        lambda names, *a, **k: asked.append(set(names)) or [])
    return asked


@pytest.mark.parametrize("process", ["plasmashell", "kwin_wayland", "python3"])
def test_refuses_to_close_the_shell_or_its_own_runtime(monkeypatch, no_real_processes, process):
    monkeypatch.setattr(apps, "_resolve", lambda n: (
        "Thing", {"display": "Thing", "exec": [process], "process": process},
        "discovered"))
    assert "can't close" in apps.close_app("thing")
    assert no_real_processes == []


def test_protected_names_never_reach_the_process_search(monkeypatch, no_real_processes):
    monkeypatch.setattr(apps, "_discovered_for_pinned", lambda e: None)
    monkeypatch.setattr(apps, "_resolve", lambda n: (
        "odd", {"launch": ("python3", "oddapp"), "process": "oddapp"}, "pinned"))
    assert apps.close_app("odd") == "odd wasn't running."
    assert no_real_processes == [{"oddapp"}]


def test_close_terminates_matching_processes(monkeypatch):
    class Proc:
        terminated = False

        def terminate(self):
            self.terminated = True

    proc = Proc()
    monkeypatch.setattr(apps, "_matching_processes", lambda names, *a, **k: [proc])
    monkeypatch.setattr(apps, "_resolve", lambda n: (
        "Spotify", {"display": "Spotify", "exec": ["spotify"], "process": "spotify"},
        "discovered"))
    assert apps.close_app("spotify") == "Closed Spotify."
    assert proc.terminated


def test_refused_signals_fall_back_to_stopping_the_systemd_scope(monkeypatch):
    """Snap apps reject SIGTERM from a confined sender; systemd stops them."""
    class Denied:
        pid = 4242

        def terminate(self):
            raise PermissionError("apparmor says no")

    stopped = []
    monkeypatch.setattr(apps, "_matching_processes", lambda names, *a, **k: [Denied()])
    monkeypatch.setattr(apps, "_app_scope", lambda pid: "snap.spotify.spotify-x.scope")

    def fake_run(argv, **kwargs):
        stopped.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(apps.subprocess, "run", fake_run)
    monkeypatch.setattr(apps, "_resolve", lambda n: (
        "Spotify", {"display": "Spotify", "exec": ["spotify"], "process": "spotify"},
        "discovered"))
    assert apps.close_app("spotify") == "Closed Spotify."
    assert stopped == [["systemctl", "--user", "stop", "--no-block",
                        "snap.spotify.spotify-x.scope"]]


def test_close_never_claims_success_when_nothing_could_be_signalled(monkeypatch):
    class Denied:
        pid = 4242

        def terminate(self):
            raise PermissionError("no")

    monkeypatch.setattr(apps, "_matching_processes", lambda names, *a, **k: [Denied()])
    monkeypatch.setattr(apps, "_app_scope", lambda pid: None)
    monkeypatch.setattr(apps, "_resolve", lambda n: (
        "Thing", {"display": "Thing", "exec": ["thing"], "process": "thing"},
        "discovered"))
    assert "wasn't permitted" in apps.close_app("thing")


def test_window_class_is_read_from_the_exec_line(tmp_path):
    entry = apps._parse_desktop_file(_desktop(
        tmp_path, "frc.desktop",
        "Name=FRC VS Code 2026\nExec=/opt/wpilib/frccode2026 --class=frc-vscode-2026\n"))
    assert entry["window_class"] == "--class=frc-vscode-2026"
    plain = apps._parse_desktop_file(_desktop(
        tmp_path, "code.desktop", "Name=Visual Studio Code\nExec=/usr/share/code/code %F\n"))
    assert plain["window_class"] is None
