"""Clicks, typing, key presses and scrolling on the real desktop.

Wayland doesn't let one program put input into another's windows (pyautogui
and xdotool only reach X11 apps), so on Linux this goes through the desktop's
Remote Desktop portal (org.freedesktop.portal.RemoteDesktop), the door KDE's
own remote-desktop tools use. The first time, KDE asks him to allow it; with
"remember" ticked the approval is kept, and the portal's restore token
(runtime/remote_desktop_token, replaced after every session, as the portal
requires) lets later sessions start without asking.

The session also casts the screen, because the portal only takes an absolute
pointer position relative to a screen-cast stream; nothing reads the stream.
KDE shows its remote-control indicator while a session is open, so a session
closes IDLE_CLOSE seconds after the last action and reopens, silently, on the
next.

Positions are fractions (0..1) of the whole desktop — what a full-screen
screenshot shows — so a point found in a screenshot maps straight to a click
whatever the scaling.

Windows has no such restriction: pyautogui does it there.

Deliberately free of any MCP import.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from pathlib import Path

from friday import plat

logger = logging.getLogger("friday-agent")

TOKEN_FILE = Path(__file__).resolve().parents[1] / "runtime" / "remote_desktop_token"
IDLE_CLOSE = 60.0
APPROVAL_WAIT = 60.0   # the first session waits for him to answer KDE's prompt
_KEY_GAP = 0.008       # between key events, so apps see every one

_BUS = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"
_RD = "org.freedesktop.portal.RemoteDesktop"
_SC = "org.freedesktop.portal.ScreenCast"
_KEYBOARD, _POINTER = 1, 2
_MONITOR = 1
_PERSIST_UNTIL_REVOKED = 2
_BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}  # evdev BTN_*


class InputError(RuntimeError):
    """Something he should hear about: not approved, no session, bad key."""


# --- keys ---------------------------------------------------------------------

# Spoken/typed key names -> X keysyms (Linux) and pyautogui names (Windows).
_NAMED_KEYS = {
    "ctrl": (0xffe3, "ctrl"), "control": (0xffe3, "ctrl"),
    "shift": (0xffe1, "shift"),
    "alt": (0xffe9, "alt"), "option": (0xffe9, "alt"),
    "super": (0xffeb, "win"), "meta": (0xffeb, "win"), "win": (0xffeb, "win"),
    "windows": (0xffeb, "win"), "cmd": (0xffeb, "win"),
    "enter": (0xff0d, "enter"), "return": (0xff0d, "enter"),
    "tab": (0xff09, "tab"),
    "esc": (0xff1b, "esc"), "escape": (0xff1b, "esc"),
    "backspace": (0xff08, "backspace"),
    "delete": (0xffff, "delete"), "del": (0xffff, "delete"),
    "space": (0x20, "space"),
    "up": (0xff52, "up"), "down": (0xff54, "down"),
    "left": (0xff51, "left"), "right": (0xff53, "right"),
    "home": (0xff50, "home"), "end": (0xff57, "end"),
    "pageup": (0xff55, "pageup"), "pagedown": (0xff56, "pagedown"),
    "insert": (0xff63, "insert"),
    "printscreen": (0xff61, "printscreen"),
    **{f"f{n}": (0xffbe + n - 1, f"f{n}") for n in range(1, 13)},
}
_ALIASES = {"page up": "pageup", "page down": "pagedown", "print screen": "printscreen",
            "arrow up": "up", "arrow down": "down", "arrow left": "left",
            "arrow right": "right", "escape key": "esc"}


def _char_keysym(ch: str) -> int:
    if ch == "\n":
        return 0xff0d
    if ch == "\t":
        return 0xff09
    cp = ord(ch)
    # Latin-1 keysyms are the code point; everything else is 0x01000000 + it.
    return cp if 0x20 <= cp <= 0x7e or 0xa0 <= cp <= 0xff else 0x01000000 + cp


def parse_keys(keys: str) -> list[list[str]]:
    """"ctrl+shift+t, enter" -> [["ctrl", "shift", "t"], ["enter"]]. Each inner
    list is pressed together; the outer list in order."""
    text = (keys or "").strip().lower()
    for spoken, name in _ALIASES.items():
        text = text.replace(spoken, name)
    text = re.sub(r"\s*\+\s*", "+", text)  # "alt + tab" is one combo
    combos = []
    for chunk in text.replace(",", " ").split():
        names = [n.strip() for n in chunk.split("+") if n.strip()]
        for name in names:
            if name not in _NAMED_KEYS and len(name) != 1:
                raise InputError(f"I don't know a key called {name}, sir.")
        if names:
            combos.append(names)
    if not combos:
        raise InputError("Which keys, sir?")
    return combos


def _keysym(name: str) -> int:
    return _NAMED_KEYS[name][0] if name in _NAMED_KEYS else _char_keysym(name)


# --- the Linux portal session -------------------------------------------------

def _load_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_token(token: str) -> None:
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
    except OSError as e:
        logger.warning("desktop input: couldn't save the restore token: %s", e)


class _PortalSession:
    """One Remote Desktop + ScreenCast portal session, opened on first use."""

    def __init__(self):
        self._lock = threading.RLock()
        self._conn = None
        self._session = ""
        self._streams: list[tuple[int, int, int, int, int]] = []  # node, x, y, w, h
        self._idle: threading.Timer | None = None

    # D-Bus plumbing

    def _method(self, interface: str, method: str, signature: str, body: tuple,
                path: str = _PATH):
        from jeepney import DBusAddress, new_method_call
        from jeepney.wrappers import DBusErrorResponse, unwrap_msg

        msg = new_method_call(DBusAddress(path, bus_name=_BUS, interface=interface),
                              method, signature, body)
        try:
            return unwrap_msg(self._conn.send_and_get_reply(msg, timeout=10))
        except DBusErrorResponse as e:
            raise InputError(f"The desktop refused {method}: {e.data or e.name}") from e

    def _request(self, interface: str, method: str, signature: str, args: tuple,
                 options: dict, timeout: float) -> dict:
        """A portal call that answers through a Request object's Response signal."""
        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus

        token = "friday_" + uuid.uuid4().hex[:12]
        sender = self._conn.unique_name.lstrip(":").replace(".", "_")
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=f"{_PATH}/request/{sender}/{token}")
        self._conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=10)
        with self._conn.filter(rule) as queue:
            self._method(interface, method, signature,
                         (*args, {**options, "handle_token": ("s", token)}))
            try:
                code, results = self._conn.recv_until_filtered(queue, timeout=timeout).body
            except TimeoutError as e:
                raise InputError("Nobody answered the desktop's remote-control prompt, sir.") from e
        if code == 1:
            raise InputError("Remote control of the desktop wasn't allowed, sir.")
        if code != 0:
            raise InputError(f"The desktop couldn't start {method}, sir.")
        return {key: value for key, (_sig, value) in results.items()}

    def _open(self) -> None:
        from jeepney.io.blocking import open_dbus_connection

        self._conn = open_dbus_connection(bus="SESSION")
        try:
            created = self._request(_RD, "CreateSession", "a{sv}", (),
                                    {"session_handle_token": ("s", "friday")}, 10)
            self._session = created["session_handle"]
            devices = {"types": ("u", _KEYBOARD | _POINTER),
                       "persist_mode": ("u", _PERSIST_UNTIL_REVOKED)}
            if token := _load_token():
                devices["restore_token"] = ("s", token)
            self._request(_RD, "SelectDevices", "oa{sv}", (self._session,), devices, 10)
            self._request(_SC, "SelectSources", "oa{sv}", (self._session,),
                          {"types": ("u", _MONITOR), "multiple": ("b", True)}, 10)
            started = self._request(_RD, "Start", "osa{sv}", (self._session, ""), {},
                                    APPROVAL_WAIT)
        except BaseException:
            self._drop()
            raise
        if started.get("restore_token"):
            _save_token(started["restore_token"])
        self._streams = []
        for node, props in started.get("streams") or []:
            x, y = (props.get("position") or ("", (0, 0)))[1]
            w, h = (props.get("size") or ("", (0, 0)))[1]
            self._streams.append((node, x, y, w, h))
        if not self._streams or not started.get("devices", 0) & _POINTER:
            self._close_session()
            raise InputError("The desktop didn't share a screen to point at, sir.")
        logger.info("desktop input: portal session open, streams=%s", self._streams)

    def _close_session(self) -> None:
        if self._conn is not None and self._session:
            try:
                self._method("org.freedesktop.portal.Session", "Close", "", (),
                             path=self._session)
            except Exception:
                pass
        self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn, self._session, self._streams = None, "", []

    def close(self) -> None:
        with self._lock:
            if self._idle:
                self._idle.cancel()
                self._idle = None
            self._close_session()

    def run(self, actions) -> None:
        """Do `actions(self)` in an open session; reopen once if the old one
        died (he revoked it from the tray, or KDE restarted)."""
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._open()
                try:
                    actions(self)
                    break
                except InputError:
                    if attempt == 2:
                        raise
                    self._drop()
            if self._idle:
                self._idle.cancel()
            self._idle = threading.Timer(IDLE_CLOSE, self.close)
            self._idle.daemon = True
            self._idle.start()

    # Input

    def _notify(self, method: str, signature: str, *args) -> None:
        self._method(_RD, method, "oa{sv}" + signature, (self._session, {}, *args))

    def move(self, fx: float, fy: float) -> None:
        left = min(s[1] for s in self._streams)
        top = min(s[2] for s in self._streams)
        right = max(s[1] + s[3] for s in self._streams)
        bottom = max(s[2] + s[4] for s in self._streams)
        gx = left + min(max(fx, 0.0), 1.0) * (right - left - 1)
        gy = top + min(max(fy, 0.0), 1.0) * (bottom - top - 1)
        for node, x, y, w, h in self._streams:
            if x <= gx < x + w and y <= gy < y + h:
                self._notify("NotifyPointerMotionAbsolute", "udd", node,
                             float(gx - x), float(gy - y))
                return
        node, x, y, w, h = self._streams[0]  # in a gap between monitors
        self._notify("NotifyPointerMotionAbsolute", "udd", node,
                     float(min(max(gx - x, 0), w - 1)), float(min(max(gy - y, 0), h - 1)))

    def button(self, button: str, count: int) -> None:
        code = _BUTTONS[button]
        for _ in range(count):
            self._notify("NotifyPointerButton", "iu", code, 1)
            self._notify("NotifyPointerButton", "iu", code, 0)
            time.sleep(0.05)

    def key(self, keysym: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeysym", "iu", keysym, 1 if pressed else 0)
        time.sleep(_KEY_GAP)

    def scroll(self, steps: int) -> None:
        self._notify("NotifyPointerAxisDiscrete", "ui", 0, steps)  # 0 = vertical


_portal = _PortalSession()


# --- public API -----------------------------------------------------------------

def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = False  # the corner would also be where a click can land
    return pyautogui


def _virtual_screen() -> tuple[int, int, int, int]:
    """Windows: left, top, width, height of the whole desktop (every monitor)."""
    import ctypes
    metric = ctypes.windll.user32.GetSystemMetrics
    return metric(76), metric(77), metric(78), metric(79)


def click(fx: float, fy: float, button: str = "left", count: int = 1) -> None:
    """Click at a fraction of the whole desktop (what a screenshot shows)."""
    if button not in _BUTTONS:
        raise InputError(f"There's no {button} mouse button, sir.")
    count = max(1, min(int(count), 3))
    logger.info("desktop input: %s click x%d at (%.3f, %.3f)", button, count, fx, fy)
    if plat.IS_WINDOWS:
        left, top, width, height = _virtual_screen()
        _pyautogui().click(left + round(fx * (width - 1)), top + round(fy * (height - 1)),
                           clicks=count, interval=0.05, button=button)
        return

    def act(s):
        s.move(fx, fy)
        time.sleep(0.03)  # let the pointer land before the press
        s.button(button, count)
    _portal.run(act)


def type_text(text: str) -> None:
    """Type `text` into whatever has focus."""
    if not text:
        return
    logger.info("desktop input: typing %d characters", len(text))
    if plat.IS_WINDOWS:
        _pyautogui().write(text, interval=0.01)
        return

    def act(s):
        for ch in text:
            sym = _char_keysym(ch)
            s.key(sym, True)
            s.key(sym, False)
    _portal.run(act)


def press(keys: str) -> None:
    """Press keys: "ctrl+s", "alt+tab", "ctrl+a, delete"."""
    combos = parse_keys(keys)
    logger.info("desktop input: pressing %s", combos)
    if plat.IS_WINDOWS:
        gui = _pyautogui()
        for combo in combos:
            gui.hotkey(*[_NAMED_KEYS[n][1] if n in _NAMED_KEYS else n for n in combo])
        return

    def act(s):
        for combo in combos:
            syms = [_keysym(n) for n in combo]
            for sym in syms:
                s.key(sym, True)
            for sym in reversed(syms):
                s.key(sym, False)
    _portal.run(act)


def scroll(steps: int) -> None:
    """Wheel-scroll at the pointer: positive is down."""
    if plat.IS_WINDOWS:
        _pyautogui().scroll(-steps)
        return
    _portal.run(lambda s: s.scroll(steps))


def close() -> None:
    """End the portal session now (it also ends by itself when idle)."""
    if not plat.IS_WINDOWS:
        _portal.close()
