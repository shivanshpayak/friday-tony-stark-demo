"""Tests for dismissal phrase detection."""
import pytest

DISMISSAL_PHRASES = [
    "that'll be all",
    "that will be all",
    "stand down",
    "go to sleep",
    "goodbye jarvis",
]


def is_dismissal(text: str) -> bool:
    """Check if text contains a dismissal phrase."""
    lower = text.lower().strip()
    return any(phrase in lower for phrase in DISMISSAL_PHRASES)


def test_exact_dismissal_phrases():
    assert is_dismissal("that'll be all")
    assert is_dismissal("stand down")
    assert is_dismissal("go to sleep")
    assert is_dismissal("goodbye jarvis")


def test_dismissal_case_insensitive():
    assert is_dismissal("Stand Down")
    assert is_dismissal("THAT'LL BE ALL")
    assert is_dismissal("Go To Sleep")


def test_dismissal_in_sentence():
    assert is_dismissal("okay jarvis, that'll be all for tonight")
    assert is_dismissal("you can stand down now")


def test_non_dismissal():
    assert not is_dismissal("what's the weather")
    assert not is_dismissal("tell me the news")
    assert not is_dismissal("hello jarvis")
