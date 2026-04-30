"""
FRIDAY Overlay — Fullscreen click-through ambient overlay.

Replaces the old floating pill widget with a subtle, screen-wide experience:
  - Accelerating ripple that reveals a blue tint on activation
  - Breathing glow aura at the top of the screen
  - Scanning pulse for thinking / booting states
  - All fully click-through — nothing blocks mouse or keyboard

See OVERLAY_REWRITE.md for the full design spec.
"""

import ctypes
import math
import threading
import time
import tkinter as tk
from enum import Enum

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
GA_ROOT = 2

# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FPS = 20
TICK_MS = 1000 // FPS

# Tint
TINT_COLOR = "#0a1428"
TINT_FADE_OUT_DURATION = 0.3

# Ripple
RIPPLE_DURATION = 0.7
RIPPLE_ACCEL = 2.5
RIPPLE_TOTAL_BANDS = 20
RIPPLE_BAND_WIDTH = 20
RIPPLE_EDGE_COLOR = "#3a8adf"

# Border aura — wraps all 4 edges of the screen
BAR_THICKNESS = 40              # px depth of the glow on each edge
BAR_COLOR = "#1a5aff"           # deepest blue
BAR_BOTTOM_GAP = 2              # leave the taskbar autohide reveal zone clear
# Tail feathering: below this normalized intensity, snap to the transparent
# color key so the glow fades out cleanly instead of ending on a dark seam.
BAR_INNER_CUTOFF = 0.08

# Per-state rhythm & intensity — each state has its own fingerprint.
# Bar alpha values are dialed back ~30% from the old top-bar-only design
# to compensate for the larger surface area of a full-screen border.
#   tint_steady: steady tint alpha (None means breathing)
#   tint_hz / tint_lo / tint_hi: tint breathing params (if not steady)
#   bar_hz / bar_lo / bar_hi: bar alpha breathing
STATE_PROFILES = {
    # Cold boot — slow, deep, heavy; feels like a system warming up
    "booting":   dict(tint_hz=0.5, tint_lo=0.08, tint_hi=0.13,
                      bar_hz=0.5,  bar_lo=0.07, bar_hi=0.25),
    # Waiting for you — gentle, patient
    "listening": dict(tint_steady=0.10,
                      bar_hz=1.0,  bar_lo=0.06, bar_hi=0.16),
    # Actively processing — fast, bright, urgent
    "thinking":  dict(tint_hz=1.4, tint_lo=0.10, tint_hi=0.16,
                      bar_hz=2.4,  bar_lo=0.13, bar_hi=0.34),
    # Talking back — steady tint, medium rhythmic bar breath
    "speaking":  dict(tint_steady=0.10,
                      bar_hz=1.7,  bar_lo=0.10, bar_hi=0.24),
}
TINT_ALPHA_TARGET = STATE_PROFILES["listening"]["tint_steady"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lerp_color(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_color_rgb(r1, g1, b1, r2, g2, b2, t):
    """Fast RGB lerp without hex parsing."""
    t = max(0.0, min(1.0, t))
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _parse_hex(c: str):
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

class OverlayState(Enum):
    SLEEPING = "sleeping"
    BOOTING = "booting"
    WAKING = "waking"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DISMISSING = "dismissing"


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

class FridayOverlay:
    """Fullscreen click-through overlay with ripple, tint, and breathing glow.

    Public API is identical to the old pill overlay so the launcher needs
    no changes.  State flags are set from any thread; _tick() on the Tk
    thread does all the actual rendering.
    """

    def __init__(self):
        self._thread = None
        self._running = False

        # Tk windows
        self._root = None
        self._tint_win = None
        self._tint_canvas = None
        self._bar_win = None
        self._bar_canvas = None

        # Pre-allocated canvas items
        self._ripple_fill = None
        self._ripple_bands = []

        # Bar: one inset border ring per pixel of thickness. This keeps corners
        # seamless and avoids edge-strip overlap/gap artifacts.
        self._bar_rings = []

        # Pre-parsed bar color
        self._bar_rgb = _parse_hex(BAR_COLOR)

        # Screen dimensions
        self._screen_w = 0
        self._screen_h = 0

        # State
        self._want_state = OverlayState.SLEEPING
        self._current_state = OverlayState.SLEEPING
        self._transition_start = 0.0
        self._loading_text = ""

        # Alpha tracking
        self._tint_alpha = 0.0
        self._bar_alpha = 0.0

        # Visibility tracking
        self._tint_visible = False
        self._bar_visible = False

    # -- Window setup (Tk thread) ------------------------------------------

    def _setup_window(self):
        self._root = tk.Tk()
        self._root.withdraw()

        self._screen_w = self._root.winfo_screenwidth()
        self._screen_h = self._root.winfo_screenheight()

        self._transparent_key = "#010101"
        bar_bg_key = "#020202"

        # ---- Tint window (fullscreen) ----
        # Leave a 2px strip at the bottom uncovered so the Windows
        # autohide taskbar can still slide up. A fullscreen topmost layered
        # window owns the reveal zone otherwise, even with WS_EX_TRANSPARENT.
        tint_h = self._screen_h - 2
        self._tint_win = tk.Toplevel(self._root)
        self._tint_win.overrideredirect(True)
        self._tint_win.attributes("-topmost", True)
        self._tint_win.geometry(f"{self._screen_w}x{tint_h}+0+0")
        self._tint_win.configure(bg=self._transparent_key)
        self._tint_win.attributes("-transparentcolor", self._transparent_key)
        self._tint_win.attributes("-alpha", 0.0)

        self._tint_canvas = tk.Canvas(
            self._tint_win,
            width=self._screen_w, height=tint_h,
            bg=self._transparent_key, highlightthickness=0,
        )
        self._tint_canvas.pack()

        # Pre-allocate ripple items
        self._ripple_fill = self._tint_canvas.create_oval(
            -10, -10, -10, -10,
            fill=TINT_COLOR, outline="", state="hidden",
        )
        self._ripple_bands = []
        for _ in range(RIPPLE_TOTAL_BANDS):
            oid = self._tint_canvas.create_oval(
                -10, -10, -10, -10,
                fill="", outline=RIPPLE_EDGE_COLOR,
                width=RIPPLE_BAND_WIDTH, state="hidden",
            )
            self._ripple_bands.append(oid)

        self._tint_win.withdraw()
        self._make_click_through(self._tint_win)

        # ---- Bar window (border aura wrapping all 4 edges) ----
        # Full-screen layered window with transparent interior. The 4 edges
        # are pre-rendered as gradient strips fading inward. Per-frame cost
        # is just one alpha attribute update — same as the old top-only bar.
        bar_h = self._screen_h - BAR_BOTTOM_GAP
        self._bar_win = tk.Toplevel(self._root)
        self._bar_win.overrideredirect(True)
        self._bar_win.attributes("-topmost", True)
        self._bar_win.geometry(f"{self._screen_w}x{bar_h}+0+0")
        self._bar_win.configure(bg=bar_bg_key)
        self._bar_win.attributes("-transparentcolor", bar_bg_key)
        self._bar_win.attributes("-alpha", 0.0)

        self._bar_canvas = tk.Canvas(
            self._bar_win,
            width=self._screen_w, height=bar_h,
            bg=bar_bg_key, highlightthickness=0,
        )
        self._bar_canvas.pack()

        # Build a continuous border gradient using concentric 1px rectangle
        # outlines. This removes corner seams caused by composing separate
        # top/bottom/side strips with slightly different geometry.
        bg_rgb = _parse_hex(bar_bg_key)
        self._bar_rings = []

        def _gradient_color(distance_from_edge: int) -> str:
            t = 1.0 - (distance_from_edge / max(1, BAR_THICKNESS - 1))
            fade = t * t * t  # cubic falloff for a softer interior tail
            if fade <= BAR_INNER_CUTOFF:
                # Exact transparent key removes the visible "inner edge" line.
                return bar_bg_key
            return _lerp_color_rgb(
                bg_rgb[0], bg_rgb[1], bg_rgb[2],
                self._bar_rgb[0], self._bar_rgb[1], self._bar_rgb[2],
                fade,
            )

        # Each inset "ring" is a 1px outline rectangle. The outer ring is the
        # strongest color; inner rings fade toward transparent.
        max_inset = max(0, BAR_THICKNESS - 1)
        for inset in range(max_inset + 1):
            color = _gradient_color(inset)
            ring = self._bar_canvas.create_rectangle(
                inset,
                inset,
                max(inset, self._screen_w - 1 - inset),
                max(inset, bar_h - 1 - inset),
                outline=color,
                width=1,
            )
            self._bar_rings.append(ring)

        self._bar_win.withdraw()
        self._make_click_through(self._bar_win)

    def _make_click_through(self, tk_window):
        tk_window.update_idletasks()
        hwnd = tk_window.winfo_id()
        hwnd = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT)
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    # -- Tint helpers (Tk thread) ------------------------------------------

    def _set_tint_alpha(self, alpha: float):
        alpha = max(0.0, min(1.0, alpha))
        if abs(alpha - self._tint_alpha) < 0.001:
            return
        self._tint_alpha = alpha
        if self._tint_win:
            self._tint_win.attributes("-alpha", alpha)

    def _set_bar_alpha(self, alpha: float):
        alpha = max(0.0, min(1.0, alpha))
        if abs(alpha - self._bar_alpha) < 0.001:
            return
        self._bar_alpha = alpha
        if self._bar_win:
            self._bar_win.attributes("-alpha", alpha)

    def _show_tint(self):
        if not self._tint_visible and self._tint_win:
            self._tint_win.deiconify()
            self._tint_visible = True

    def _hide_tint(self):
        if self._tint_visible and self._tint_win:
            self._tint_win.withdraw()
            self._tint_visible = False
            self._tint_alpha = 0.0

    def _show_bar(self):
        if not self._bar_visible and self._bar_win:
            self._bar_win.deiconify()
            self._bar_visible = True

    def _hide_bar(self):
        if self._bar_visible and self._bar_win:
            self._bar_win.withdraw()
            self._bar_visible = False
            self._bar_alpha = 0.0

    # -- Ripple (Tk thread) ------------------------------------------------

    def _draw_ripple(self, progress: float):
        cx = self._screen_w / 2
        cy = self._screen_h / 2
        max_radius = math.hypot(cx, cy)
        radius = max_radius * progress

        if radius < 1:
            return

        half = RIPPLE_TOTAL_BANDS // 2
        total_spread = RIPPLE_TOTAL_BANDS * RIPPLE_BAND_WIDTH

        inner_r = max(1, radius - total_spread * 0.5)
        self._tint_canvas.coords(
            self._ripple_fill,
            cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r,
        )
        self._tint_canvas.itemconfig(self._ripple_fill, state="normal")

        for idx, oid in enumerate(self._ripple_bands):
            offset = (idx - half) * RIPPLE_BAND_WIDTH
            band_r = radius + offset

            if band_r < 1:
                self._tint_canvas.itemconfig(oid, state="hidden")
                continue

            dist_from_center = abs(idx - half) / max(half, 1)
            if idx < half:
                color = _lerp_color(RIPPLE_EDGE_COLOR, TINT_COLOR, dist_from_center)
            else:
                color = _lerp_color(RIPPLE_EDGE_COLOR, self._transparent_key, dist_from_center)

            self._tint_canvas.coords(
                oid, cx - band_r, cy - band_r, cx + band_r, cy + band_r,
            )
            self._tint_canvas.itemconfig(
                oid, outline=color, width=RIPPLE_BAND_WIDTH, state="normal",
            )

    def _hide_ripple_items(self):
        self._tint_canvas.itemconfig(self._ripple_fill, state="hidden")
        for oid in self._ripple_bands:
            self._tint_canvas.itemconfig(oid, state="hidden")

    def _finalize_ripple(self):
        self._hide_ripple_items()
        self._tint_canvas.configure(bg=TINT_COLOR)
        self._tint_win.attributes("-transparentcolor", "")

    def _reset_tint_canvas(self):
        self._hide_ripple_items()
        self._tint_canvas.configure(bg=self._transparent_key)
        self._tint_win.attributes("-transparentcolor", self._transparent_key)

    # -- Bar animations (Tk thread) ----------------------------------------

    def _breath(self, hz: float, lo: float, hi: float) -> float:
        """Sine-based alpha breath at `hz` cycles/sec between lo and hi."""
        t = time.time() * hz * 2 * math.pi
        return lo + (hi - lo) * (0.5 + 0.5 * math.sin(t))

    def _apply_profile(self, profile: dict):
        """Apply a state profile's per-frame tint + bar animation."""
        # Tint
        if "tint_steady" in profile:
            self._set_tint_alpha(profile["tint_steady"])
        else:
            self._set_tint_alpha(
                self._breath(profile["tint_hz"], profile["tint_lo"], profile["tint_hi"])
            )
        # Bar
        self._set_bar_alpha(
            self._breath(profile["bar_hz"], profile["bar_lo"], profile["bar_hi"])
        )

    # -- Main tick (Tk thread) ---------------------------------------------

    def _tick(self):
        if not self._running:
            return

        now = time.time()
        want = self._want_state
        cur = self._current_state

        # ---- State transitions ----
        if want != cur:
            self._transition_start = now
            prev = cur
            self._current_state = want

            if want == OverlayState.SLEEPING:
                self._hide_tint()
                self._hide_bar()
                self._reset_tint_canvas()

            elif want == OverlayState.BOOTING:
                self._reset_tint_canvas()
                self._tint_canvas.configure(bg=TINT_COLOR)
                self._tint_win.attributes("-transparentcolor", "")
                self._set_tint_alpha(0.0)
                self._show_tint()
                self._show_bar()

            elif want == OverlayState.WAKING:
                self._reset_tint_canvas()
                self._set_tint_alpha(TINT_ALPHA_TARGET)
                self._show_tint()

            elif want == OverlayState.LISTENING:
                if prev == OverlayState.WAKING:
                    self._finalize_ripple()
                elif prev != OverlayState.SPEAKING and prev != OverlayState.THINKING:
                    self._tint_canvas.configure(bg=TINT_COLOR)
                    self._tint_win.attributes("-transparentcolor", "")
                self._set_tint_alpha(TINT_ALPHA_TARGET)
                self._show_tint()
                self._show_bar()

            elif want == OverlayState.THINKING:
                self._show_bar()
                self._show_tint()

            elif want == OverlayState.SPEAKING:
                self._set_tint_alpha(TINT_ALPHA_TARGET)
                self._show_tint()
                self._show_bar()

            elif want == OverlayState.DISMISSING:
                pass

        # ---- Per-frame animation ----
        elapsed = now - self._transition_start

        if self._current_state == OverlayState.SLEEPING:
            pass

        elif self._current_state == OverlayState.BOOTING:
            profile = STATE_PROFILES["booting"]
            if elapsed < 1.0:
                # Fade in to the profile's peaks, then start breathing
                frac = elapsed / 1.0
                self._set_tint_alpha(profile["tint_hi"] * frac)
                self._set_bar_alpha(profile["bar_hi"] * frac)
            else:
                self._apply_profile(profile)

        elif self._current_state == OverlayState.WAKING:
            if elapsed < RIPPLE_DURATION:
                progress = (elapsed / RIPPLE_DURATION) ** RIPPLE_ACCEL
                self._draw_ripple(progress)
            else:
                self._want_state = OverlayState.LISTENING

        elif self._current_state == OverlayState.LISTENING:
            self._apply_profile(STATE_PROFILES["listening"])

        elif self._current_state == OverlayState.THINKING:
            self._apply_profile(STATE_PROFILES["thinking"])

        elif self._current_state == OverlayState.SPEAKING:
            self._apply_profile(STATE_PROFILES["speaking"])

        elif self._current_state == OverlayState.DISMISSING:
            if elapsed < TINT_FADE_OUT_DURATION:
                frac = 1.0 - (elapsed / TINT_FADE_OUT_DURATION)
                self._set_tint_alpha(TINT_ALPHA_TARGET * frac)
                self._set_bar_alpha(STATE_PROFILES["speaking"]["bar_hi"] * frac)
            else:
                self._hide_tint()
                self._hide_bar()
                self._reset_tint_canvas()
                self._current_state = OverlayState.SLEEPING
                self._want_state = OverlayState.SLEEPING

        self._root.after(TICK_MS, self._tick)

    # -- Public API (thread-safe, just sets flags) -------------------------

    def show(self):
        self._want_state = OverlayState.LISTENING

    def show_loading(self, text: str = ""):
        self._loading_text = text
        lower = text.lower()
        if "waking" in lower or "wake" in lower:
            self._want_state = OverlayState.WAKING
        elif "thinking" in lower or "processing" in lower:
            self._want_state = OverlayState.THINKING
        else:
            self._want_state = OverlayState.BOOTING

    def hide_loading(self):
        self._want_state = OverlayState.SPEAKING

    def hide(self):
        if self._current_state == OverlayState.SLEEPING:
            return
        self._want_state = OverlayState.DISMISSING

    # -- Lifecycle ---------------------------------------------------------

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._setup_window()
        self._tick()
        self._root.mainloop()

    def stop(self):
        self._running = False
        self._want_state = OverlayState.SLEEPING
        if self._root:
            try:
                self._root.quit()
            except Exception:
                pass
