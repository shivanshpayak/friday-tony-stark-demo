"""
FRIDAY Overlay — Linux renderer (Qt).

Same look and state machine as the Tk/Win32 overlay in friday_overlay.py:
ripple on wake, screen tint, breathing border aura. Fully click-through.

Runs as its own process because Qt wants the main thread and the launcher's
main thread belongs to asyncio. The launcher (friday_overlay.FridayOverlay)
writes one state name per line to our stdin; EOF means quit.

It runs through XWayland (QT_QPA_PLATFORM=xcb, set by the parent): a native
Wayland client can't position itself, stay above other windows, or skip the
window manager, and an X11 override-redirect window can do all three.
"""

import math
import sys
import threading
import time

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

from friday_overlay import (
    BAR_COLOR, BAR_INNER_CUTOFF, BAR_THICKNESS, RIPPLE_ACCEL, RIPPLE_BAND_WIDTH,
    RIPPLE_DURATION, RIPPLE_EDGE_COLOR, RIPPLE_TOTAL_BANDS, STATE_PROFILES,
    TICK_MS, TINT_ALPHA_TARGET, TINT_COLOR, TINT_FADE_OUT_DURATION, OverlayState,
)

# Tk composites the whole window at `alpha`; here alpha is per-pixel, and a
# translucent fill over a desktop reads fainter than a layered window does.
BAR_ALPHA_GAIN = 2.2


def _breath(hz: float, lo: float, hi: float) -> float:
    t = time.time() * hz * 2 * math.pi
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(t))


class OverlayWindow(QWidget):
    def __init__(self, geometry):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.X11BypassWindowManagerHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(geometry)

        self.want_state = OverlayState.SLEEPING   # written by the stdin thread
        self._state = OverlayState.SLEEPING
        self._transition_start = 0.0
        self._tint_alpha = 0.0
        self._bar_alpha = 0.0
        self._ripple = None          # ripple progress 0..1 while WAKING
        self._border = None          # cached border aura pixmap

        self._tint = QColor(TINT_COLOR)
        self._bar = QColor(BAR_COLOR)
        self._edge = QColor(RIPPLE_EDGE_COLOR)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # -- state machine (mirrors FridayOverlay._tick) -----------------------

    def _apply_profile(self, profile: dict):
        if "tint_steady" in profile:
            self._tint_alpha = profile["tint_steady"]
        else:
            self._tint_alpha = _breath(
                profile["tint_hz"], profile["tint_lo"], profile["tint_hi"])
        self._bar_alpha = _breath(
            profile["bar_hz"], profile["bar_lo"], profile["bar_hi"])

    def _tick(self):
        now = time.time()
        want = self.want_state
        if want != self._state:
            self._transition_start = now
            self._state = want
            self._ripple = None
            if want == OverlayState.SLEEPING:
                self._tint_alpha = self._bar_alpha = 0.0
            elif want == OverlayState.WAKING:
                self._tint_alpha, self._bar_alpha = TINT_ALPHA_TARGET, 0.0

        elapsed = now - self._transition_start
        state = self._state

        if state == OverlayState.SLEEPING:
            if self.isVisible():
                self.hide()
            return
        if state == OverlayState.BOOTING:
            profile = STATE_PROFILES["booting"]
            if elapsed < 1.0:
                self._tint_alpha = profile["tint_hi"] * elapsed
                self._bar_alpha = profile["bar_hi"] * elapsed
            else:
                self._apply_profile(profile)
        elif state == OverlayState.WAKING:
            if elapsed < RIPPLE_DURATION:
                self._ripple = (elapsed / RIPPLE_DURATION) ** RIPPLE_ACCEL
            else:
                self.want_state = OverlayState.LISTENING
        elif state == OverlayState.DISMISSING:
            if elapsed < TINT_FADE_OUT_DURATION:
                frac = 1.0 - elapsed / TINT_FADE_OUT_DURATION
                self._tint_alpha = TINT_ALPHA_TARGET * frac
                self._bar_alpha = STATE_PROFILES["speaking"]["bar_hi"] * frac
            else:
                self.want_state = OverlayState.SLEEPING
        else:
            self._apply_profile(STATE_PROFILES[state.value])

        if not self.isVisible():
            self.show()
        self.update()

    # -- rendering ---------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        w, h = self.width(), self.height()

        tint = QColor(self._tint)
        tint.setAlphaF(max(0.0, min(1.0, self._tint_alpha)))

        if self._ripple is not None:
            self._paint_ripple(painter, w, h, tint)
        else:
            painter.fillRect(0, 0, w, h, tint)
            self._paint_border(painter, w, h)
        painter.end()

    def _paint_ripple(self, painter, w, h, tint):
        """Tint spreading from the centre behind a bright expanding edge."""
        center = QPointF(w / 2, h / 2)
        radius = math.hypot(w / 2, h / 2) * self._ripple
        spread = RIPPLE_TOTAL_BANDS * RIPPLE_BAND_WIDTH / 2
        outer = radius + spread
        if outer < 1:
            return
        edge = QColor(self._edge)
        edge.setAlphaF(min(1.0, tint.alphaF() * 4))
        clear = QColor(self._edge)
        clear.setAlphaF(0.0)

        gradient = QRadialGradient(center, outer)
        gradient.setColorAt(0.0, tint)
        gradient.setColorAt(max(0.0, (radius - spread) / outer), tint)
        gradient.setColorAt(max(0.0, min(1.0, radius / outer)), edge)
        gradient.setColorAt(1.0, clear)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, outer, outer)

    def _border_pixmap(self, w, h) -> QPixmap:
        """Full-strength border aura, rendered once per window size: concentric
        1px rings fading inward with a cubic falloff. Per frame it is just one
        blit at the current opacity."""
        if self._border is not None and self._border.width() == w \
                and self._border.height() == h:
            return self._border
        pixmap = QPixmap(w, h)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        color = QColor(self._bar)
        for inset in range(BAR_THICKNESS):
            fade = (1.0 - inset / max(1, BAR_THICKNESS - 1)) ** 3
            if fade <= BAR_INNER_CUTOFF:
                break
            color.setAlphaF(fade)
            painter.setPen(color)
            painter.drawRect(inset, inset, w - 1 - 2 * inset, h - 1 - 2 * inset)
        painter.end()
        self._border = pixmap
        return pixmap

    def _paint_border(self, painter, w, h):
        strength = min(1.0, self._bar_alpha * BAR_ALPHA_GAIN)
        if strength <= 0.0:
            return
        painter.setOpacity(strength)
        painter.drawPixmap(0, 0, self._border_pixmap(w, h))
        painter.setOpacity(1.0)


def _read_states(window: OverlayWindow, app: QApplication):
    for line in sys.stdin:
        name = line.strip().lower()
        if name == "quit":
            break
        try:
            window.want_state = OverlayState(name)
        except ValueError:
            continue
    # EOF: the launcher is gone — never outlive it.
    QTimer.singleShot(0, app, app.quit)


def main():
    app = QApplication(sys.argv)
    screen = QGuiApplication.primaryScreen()
    window = OverlayWindow(screen.geometry())
    threading.Thread(target=_read_states, args=(window, app), daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
