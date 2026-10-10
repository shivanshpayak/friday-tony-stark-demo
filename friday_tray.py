"""
JARVIS tray button (Linux).

A system-tray icon that shows what JARVIS is doing and gives one-click access:

    left click    type a request (reply arrives as a notification, never spoken)
    middle click  talk (same as the push-to-talk shortcut)
    right click   menu: talk, type, quiet mode, restart, quit

The icon is the state: a bright ring while a conversation is live, a dim ring
while asleep, a struck-through grey ring in quiet mode, red when the launcher
can't be reached.

Runs as its own process, started by the launcher (Qt wants the main thread;
the launcher's belongs to asyncio). It holds no state of its own — it polls
the launcher's control socket and sends the same commands friday_ctl does.
It exits when the launcher closes its stdin.
"""

import os
import signal
import sys
import threading

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from friday import plat, quiet
from friday_ctl import _SIGNALS, _running_pid, ask, request

POLL_MS = 2000

# state -> (ring colour, core colour or None, struck through)
_LOOKS = {
    "active":   ("#4fc3ff", "#bfe9ff", False),
    "sleeping": ("#2f7fb8", None, False),
    "quiet":    ("#8a8f98", None, True),
    "offline":  ("#c0504d", None, False),
}
_TOOLTIPS = {
    "active": "JARVIS — listening",
    "sleeping": "JARVIS — say “Hey Jarvis”, or click to type",
    "quiet": "JARVIS — quiet mode (text only)",
    "offline": "JARVIS — not running",
}


def draw_icon(state: str, size: int = 64) -> QIcon:
    """Arc-reactor style ring for `state`, drawn so it needs no image files."""
    ring, core, struck = _LOOKS[state]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    center = QPointF(size / 2, size / 2)

    painter.setPen(QPen(QColor(ring), size * 0.11))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, size * 0.36, size * 0.36)
    painter.setPen(QPen(QColor(ring), size * 0.05))
    painter.drawEllipse(center, size * 0.20, size * 0.20)
    if core:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(core))
        painter.drawEllipse(center, size * 0.11, size * 0.11)
    if struck:
        pen = QPen(QColor(ring), size * 0.10)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(size * 0.16, size * 0.84), QPointF(size * 0.84, size * 0.16))
    painter.end()
    return QIcon(pixmap)


def resolve_state(status: dict) -> str:
    """Collapse a control-socket status reply into one of the icon states."""
    if status.get("error") or not status.get("agent_alive", True):
        return "offline"
    if status.get("quiet"):
        return "quiet"
    return "active" if status.get("state") == "active" else "sleeping"


# Palette shared with the screen overlay (friday_overlay.py).
_PANEL_BG = QColor(10, 20, 40, 244)
_PANEL_EDGE_TOP = QColor("#3a8adf")
_PANEL_EDGE_BOTTOM = QColor("#1a5aff")
_TEXT = "#e6f1ff"
_TEXT_DIM = "#7f93b5"
_ACCENT = "#4fc3ff"
_ERROR = "#ff8a80"
_RADIUS = 18


class PromptPanel(QWidget):
    """The type-to-JARVIS panel: one line in, the reply underneath.

    Frameless and translucent, in the overlay's colours. Enter sends, Esc
    closes. The reply appears in place; if the panel was closed first, the
    caller shows it as a notification instead.
    """

    submitted = Signal(str)

    def __init__(self, icon: QIcon):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("JARVIS")
        self.setFixedWidth(620)
        self._busy = False
        self._was_active = False
        self._dots = 0

        badge = QLabel()
        badge.setPixmap(icon.pixmap(34, 34))
        title = QLabel("J.A.R.V.I.S.")
        title_font = QFont()
        title_font.setPointSize(9)
        title_font.setBold(True)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.0)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {_ACCENT};")
        self._mode = QLabel("")
        self._mode.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 9pt;")

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(badge)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._mode)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask me anything, sir…")
        self._input.setFrame(False)
        self._input.setStyleSheet(
            f"QLineEdit {{ background: rgba(255,255,255,14); color: {_TEXT};"
            f" border: 1px solid rgba(79,195,255,70); border-radius: 10px;"
            f" padding: 11px 14px; font-size: 14pt;"
            f" selection-background-color: {_ACCENT}; selection-color: #06101f; }}"
            f"QLineEdit:focus {{ border: 1px solid {_ACCENT}; }}"
            f"QLineEdit:disabled {{ color: {_TEXT_DIM}; }}"
        )
        self._input.returnPressed.connect(self._submit)

        self._reply = QLabel("")
        self._reply.setWordWrap(True)
        self._reply.setTextFormat(Qt.TextFormat.PlainText)
        self._reply.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._reply.setStyleSheet(
            f"color: {_TEXT}; font-size: 12pt; padding: 4px 4px 0 4px;")
        self._reply.hide()

        self._hint = QLabel("Enter to send  ·  Esc to close")
        self._hint.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 8.5pt;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self._input)
        layout.addWidget(self._reply)
        layout.addWidget(self._hint)

        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._tick)

    # -- painting ----------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, _RADIUS, _RADIUS)
        painter.fillPath(path, _PANEL_BG)
        edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        edge.setColorAt(0.0, _PANEL_EDGE_TOP)
        edge.setColorAt(1.0, _PANEL_EDGE_BOTTOM)
        painter.setPen(QPen(edge, 1.6))
        painter.drawPath(path)
        painter.end()

    # -- behaviour ---------------------------------------------------------

    def open(self, quiet_mode: bool):
        self._mode.setText("QUIET MODE" if quiet_mode else "")
        if not self._busy:
            self._input.clear()
            self._reply.hide()
            self._hint.show()
        self._fit()
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus()

    def _fit(self):
        # adjustSize() over-allocates for a word-wrapped label; ask the layout
        # for the exact height at our fixed width instead.
        # With the reply hidden nothing in the layout wraps, and Qt then
        # reports "no height-for-width" as -1 — which made a zero-height,
        # invisible window. Fall back to the plain size hint.
        layout = self.layout()
        layout.activate()
        height = layout.totalHeightForWidth(self.width())
        if height <= 0:
            height = layout.totalSizeHint().height()
        self.setFixedHeight(max(height, 120))

    def _submit(self):
        text = self._input.text().strip()
        if not text or self._busy:
            return
        self._busy = True
        self._input.setEnabled(False)
        self._hint.hide()
        self._dots = 0
        self._set_reply("Thinking", dim=True)
        self._pulse.start(350)
        self.submitted.emit(text)

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        self._reply.setText("Thinking" + "." * self._dots)

    def _set_reply(self, text: str, *, dim: bool = False, error: bool = False):
        color = _ERROR if error else (_TEXT_DIM if dim else _TEXT)
        self._reply.setStyleSheet(
            f"color: {color}; font-size: 12pt; padding: 4px 4px 0 4px;")
        self._reply.setText(text)
        self._reply.show()
        self._fit()

    def show_reply(self, text: str, is_error: bool):
        """Display the answer and hand the line back for a follow-up."""
        self._pulse.stop()
        self._busy = False
        self._set_reply(text, error=is_error)
        self._input.setEnabled(True)
        self._input.clear()
        self._input.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def changeEvent(self, event):
        # Click-away closes it, like KRunner — but only once it has actually
        # had focus, and never while an answer is on its way.
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and not self._busy and self.isVisible():
                self._was_active = False
                self.hide()
        super().changeEvent(event)


class Tray(QSystemTrayIcon):
    _replied = Signal(str, str, bool)   # title, body, is_error

    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self._state = None
        self._icons = {name: draw_icon(name) for name in _LOOKS}

        menu = QMenu()
        self._talk = QAction("Talk", menu)
        self._talk.triggered.connect(self.talk)
        type_action = QAction("Type to JARVIS…", menu)
        type_action.triggered.connect(self.type_request)
        self._quiet = QAction("Quiet mode", menu)
        self._quiet.setCheckable(True)
        self._quiet.triggered.connect(self.toggle_quiet)
        self._quiet_auto = QAction("Quiet mode: back to automatic", menu)
        self._quiet_auto.triggered.connect(lambda: self._set_quiet(None))
        restart = QAction("Restart JARVIS", menu)
        restart.triggered.connect(lambda: self._signal("restart"))
        quit_action = QAction("Quit JARVIS", menu)
        quit_action.triggered.connect(lambda: self._signal("kill"))
        for action in (self._talk, type_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self._quiet)
        menu.addAction(self._quiet_auto)
        menu.addSeparator()
        menu.addAction(restart)
        menu.addAction(quit_action)
        self._menu = menu            # keep a reference; Qt doesn't own it
        self.setContextMenu(menu)

        self._panel = PromptPanel(self._icons["active"])
        self._panel.submitted.connect(self._ask)
        self._quiet_now = False

        self.activated.connect(self._on_activated)
        self._replied.connect(self._show_reply)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(POLL_MS)
        self.refresh()
        self.show()

    # -- state -------------------------------------------------------------

    def refresh(self):
        status = request({"cmd": "status"}, timeout=1.5)
        state = resolve_state(status)
        self._quiet_now = bool(status.get("quiet"))
        self._quiet.setChecked(self._quiet_now)
        self._quiet_auto.setVisible(status.get("reason") == "manual")
        self._talk.setEnabled(state in ("active", "sleeping"))
        if state != self._state:
            self._state = state
            self.setIcon(self._icons[state])
            self.setToolTip(_TOOLTIPS[state])

    # -- actions -----------------------------------------------------------

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.type_request()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.talk()

    def _signal(self, command: str):
        pid = _running_pid()
        if pid is None:
            plat.notify("JARVIS is not running", "Start it with: uv run friday_start",
                        icon="dialog-warning")
            return
        os.kill(pid, getattr(signal, _SIGNALS[command]))

    def talk(self):
        self._signal("ptt")   # the launcher itself refuses this in quiet mode

    def type_request(self):
        if self._panel.isVisible():
            self._panel.hide()      # the tray button toggles the panel
        else:
            self._panel.open(self._quiet_now)

    def _ask(self, text: str):
        def work():
            reply, error = ask(text)
            title = text if len(text) <= 60 else text[:57] + "…"
            if reply:
                self._replied.emit(title, reply, False)
            else:
                self._replied.emit("JARVIS couldn't answer", error or "No reply.", True)

        threading.Thread(target=work, daemon=True).start()

    def _show_reply(self, title: str, body: str, is_error: bool):
        if self._panel.isVisible():
            self._panel.show_reply(body, is_error)
            return
        # Panel was closed before the answer arrived — don't lose it.
        self._panel.show_reply(body, is_error)
        plat.notify(title, body, icon="dialog-warning" if is_error else "audio-input-microphone")

    def _set_quiet(self, value):
        state = quiet.set_override(value)
        plat.notify("JARVIS quiet mode " + ("on" if state["quiet"] else "off"),
                    "Text only — I won't speak." if state["quiet"] else "Voice is back on.")
        self.refresh()

    def toggle_quiet(self, checked: bool):
        self._set_quiet(bool(checked))


def _quit_when_launcher_goes(app: QApplication):
    for _ in sys.stdin:
        pass
    QTimer.singleShot(0, app, app.quit)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS")
    app.setQuitOnLastWindowClosed(False)   # the input dialog closing isn't "quit"
    tray = Tray(app)                       # noqa: F841 — must stay referenced
    if "--standalone" not in sys.argv:
        threading.Thread(target=_quit_when_launcher_goes, args=(app,), daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
