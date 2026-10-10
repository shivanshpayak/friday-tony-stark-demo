"""Tray button: the icon must tell the truth about what JARVIS is doing."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux tray")

friday_tray = pytest.importorskip("friday_tray")


@pytest.mark.parametrize("status, expected", [
    ({"error": "JARVIS is not reachable"}, "offline"),
    ({"agent_alive": False, "state": "sleeping", "quiet": False}, "offline"),
    ({"agent_alive": True, "state": "sleeping", "quiet": False}, "sleeping"),
    ({"agent_alive": True, "state": "active", "quiet": False}, "active"),
    # Quiet wins over everything live: it is the state the user must be able
    # to trust at a glance in class.
    ({"agent_alive": True, "state": "sleeping", "quiet": True}, "quiet"),
    ({"agent_alive": True, "state": "active", "quiet": True}, "quiet"),
])
def test_status_maps_to_icon_state(status, expected):
    assert friday_tray.resolve_state(status) == expected


def test_every_state_has_a_look_and_a_tooltip():
    assert set(friday_tray._LOOKS) == set(friday_tray._TOOLTIPS)


def test_prompt_panel_never_opens_with_zero_height(monkeypatch):
    """With the reply hidden nothing wraps, Qt reports height-for-width as -1,
    and the panel opened as an invisible zero-height window."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    panel = friday_tray.PromptPanel(friday_tray.draw_icon("active"))
    panel._fit()
    empty_height = panel.height()
    assert empty_height >= 120

    panel._set_reply("A reply long enough to wrap onto a second line. " * 4)
    assert panel.height() > empty_height
    app.processEvents()
