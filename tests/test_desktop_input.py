"""Desktop control: key parsing, where a click lands, the portal session's
reopen-once, Gemini's answer parsing, and what the tools tell him. The portal
and Gemini are faked — nothing here moves the real pointer."""
import sys

import pytest

import friday.desktop_input as di
from friday.tools import desktop
from friday.tools.screen import parse_point

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="Wayland portal path")


# --- keys ---------------------------------------------------------------------

def test_parse_keys_combos_and_sequences():
    assert di.parse_keys("ctrl+shift+t") == [["ctrl", "shift", "t"]]
    assert di.parse_keys("ctrl+a, delete") == [["ctrl", "a"], ["delete"]]
    assert di.parse_keys("Page Down") == [["pagedown"]]
    assert di.parse_keys("alt + tab") == [["alt", "tab"]]


def test_parse_keys_rejects_what_it_cant_press():
    with pytest.raises(di.InputError, match="hyperdrive"):
        di.parse_keys("ctrl+hyperdrive")
    with pytest.raises(di.InputError):
        di.parse_keys("  ")


def test_characters_map_to_keysyms():
    assert di._char_keysym("a") == ord("a")
    assert di._char_keysym("A") == ord("A")
    assert di._char_keysym("é") == 0xe9                # Latin-1: the code point
    assert di._char_keysym("✓") == 0x01000000 + 0x2713  # Unicode keysym
    assert di._char_keysym("\n") == 0xff0d              # Return


# --- the portal session (faked) -------------------------------------------------

class FakeSession(di._PortalSession):
    """A session whose D-Bus side is a list of the Notify calls it would make."""

    def __init__(self, streams):
        super().__init__()
        self.sent = []
        self.opens = 0
        self.dead = False
        self._streams = streams

    def _open(self):
        self.opens += 1
        self._conn = object()

    def _drop(self):
        self._conn = None

    def _notify(self, method, signature, *args):
        if self.dead:
            self.dead = False
            raise di.InputError("session closed")
        self.sent.append((method, *args))


@pytest.fixture
def portal(monkeypatch):
    session = FakeSession([(69, 0, 0, 1920, 1200)])
    monkeypatch.setattr(di, "_portal", session)
    monkeypatch.setattr(di.plat, "IS_WINDOWS", False)
    monkeypatch.setattr(di, "_KEY_GAP", 0)
    monkeypatch.setattr(di.time, "sleep", lambda s: None)
    yield session
    session.close()


@linux_only
def test_click_moves_then_presses_and_releases(portal):
    di.click(0.5, 0.25)
    assert portal.sent == [
        ("NotifyPointerMotionAbsolute", 69, pytest.approx(959.5), pytest.approx(299.75)),
        ("NotifyPointerButton", 0x110, 1),
        ("NotifyPointerButton", 0x110, 0),
    ]


@linux_only
def test_double_right_click(portal):
    di.click(0.1, 0.1, button="right", count=2)
    buttons = [c for c in portal.sent if c[0] == "NotifyPointerButton"]
    assert buttons == [("NotifyPointerButton", 0x111, 1), ("NotifyPointerButton", 0x111, 0)] * 2


def test_unknown_button_is_refused(portal):
    with pytest.raises(di.InputError):
        di.click(0.5, 0.5, button="side")


@linux_only
def test_a_point_on_the_second_monitor_goes_to_its_stream(portal):
    portal._streams = [(1, 0, 0, 1920, 1080), (2, 1920, 0, 1280, 1024)]
    di.click(0.9, 0.5)   # x = 0.9 * 3199 = 2879 -> 959 into the second monitor
    _, node, x, y = portal.sent[0]
    assert node == 2 and x == pytest.approx(2879.1 - 1920) and y == pytest.approx(539.5)


@linux_only
def test_typing_presses_and_releases_each_character(portal):
    di.type_text("Hi")
    assert portal.sent == [
        ("NotifyKeyboardKeysym", ord("H"), 1), ("NotifyKeyboardKeysym", ord("H"), 0),
        ("NotifyKeyboardKeysym", ord("i"), 1), ("NotifyKeyboardKeysym", ord("i"), 0),
    ]


@linux_only
def test_a_shortcut_holds_modifiers_and_releases_in_reverse(portal):
    di.press("ctrl+s")
    assert portal.sent == [
        ("NotifyKeyboardKeysym", 0xffe3, 1), ("NotifyKeyboardKeysym", ord("s"), 1),
        ("NotifyKeyboardKeysym", ord("s"), 0), ("NotifyKeyboardKeysym", 0xffe3, 0),
    ]


@linux_only
def test_a_dead_session_is_reopened_once(portal):
    di.press("enter")
    assert portal.opens == 1
    portal.dead = True       # he revoked it from the tray
    di.press("enter")
    assert portal.opens == 2
    assert len(portal.sent) == 4


@linux_only
def test_scroll_down_is_a_positive_vertical_step(portal):
    di.scroll(10)
    assert portal.sent == [("NotifyPointerAxisDiscrete", 0, 10)]


def test_restore_token_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "TOKEN_FILE", tmp_path / "runtime" / "token")
    assert di._load_token() == ""
    di._save_token("abc-123")
    assert di._load_token() == "abc-123"
    assert (tmp_path / "runtime" / "token").stat().st_mode & 0o077 == 0


# --- Gemini's answer -----------------------------------------------------------------

def test_parse_point_takes_the_box_centre():
    assert parse_point('{"box_2d": [400, 500, 420, 560]}') == (0.53, 0.41)
    assert parse_point('[{"box_2d": [0, 0, 1000, 1000]}]') == (0.5, 0.5)
    assert parse_point('```json\n{"box_2d": [100, 100, 200, 300]}\n```') == (0.2, 0.15)


def test_parse_point_when_it_isnt_there():
    assert parse_point('{"box_2d": null}') is None
    assert parse_point("no idea") is None
    assert parse_point('{"box_2d": [0, 0, 1200, 10]}') is None
    assert parse_point('{"box_2d": [1, 2, 3]}') is None


# --- the tools --------------------------------------------------------------------------

@pytest.fixture
def actions(monkeypatch):
    done = []
    monkeypatch.setattr(desktop.desktop_input, "click",
                        lambda x, y, button="left", count=1: done.append(("click", x, y, button, count)))
    monkeypatch.setattr(desktop.desktop_input, "type_text", lambda t: done.append(("type", t)))
    monkeypatch.setattr(desktop.desktop_input, "press", lambda k: done.append(("press", k)))
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    return done


def test_click_on_what_he_described(actions, monkeypatch):
    monkeypatch.setattr(desktop, "locate_on_screen", lambda t: (0.25, 0.75))
    assert desktop.click_on_screen("the Submit button") == "Clicked the Submit button."
    assert actions == [("click", 0.25, 0.75, "left", 1)]


def test_nothing_is_clicked_when_it_isnt_on_screen(actions, monkeypatch):
    monkeypatch.setattr(desktop, "locate_on_screen", lambda t: None)
    assert desktop.click_on_screen("the Submit button") == "I can't see the Submit button on the screen, sir."
    assert desktop.type_text("hello", into="the search bar").startswith("I can't see")
    assert actions == []


def test_type_into_a_field_then_enter(actions, monkeypatch):
    monkeypatch.setattr(desktop, "locate_on_screen", lambda t: (0.5, 0.1))
    assert desktop.type_text("cats", into="the search bar", enter=True) == "Typed it. Pressed Enter."
    assert actions == [("click", 0.5, 0.1, "left", 1), ("type", "cats"), ("press", "enter")]


def test_vision_and_portal_failures_are_said_plainly(actions, monkeypatch):
    def broken(target):
        raise desktop.VisionError("GOOGLE_API_KEY not set.")
    monkeypatch.setattr(desktop, "locate_on_screen", broken)
    assert "couldn't read the screen" in desktop.click_on_screen("Submit")

    def refused(keys):
        raise di.InputError("Remote control of the desktop wasn't allowed, sir.")
    monkeypatch.setattr(desktop.desktop_input, "press", refused)
    assert desktop.press_keys("ctrl+s") == "Remote control of the desktop wasn't allowed, sir."
