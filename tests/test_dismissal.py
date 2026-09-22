"""Tests for dismissal phrase detection.

These exercise the REAL matcher (agent_friday._is_dismissal) against the REAL
phrase list (friday.config.DISMISSAL_PHRASES). An earlier version of this file
tested a local copy of the matcher, so it never noticed that the live one
lowercased the transcript but not the phrases ("Kill Yourself", "Dissmissed"
could never match) and choked on Deepgram's punctuation ("Goodbye, Jarvis.").
"""
import pytest

from agent_friday import _is_dismissal
from friday.config import DISMISSAL_PHRASES


def test_exact_dismissal_phrases():
    assert _is_dismissal("that'll be all")
    assert _is_dismissal("stand down")
    assert _is_dismissal("go to sleep")
    assert _is_dismissal("goodbye jarvis")


def test_dismissal_case_insensitive():
    assert _is_dismissal("Stand Down")
    assert _is_dismissal("THAT'LL BE ALL")
    assert _is_dismissal("Go To Sleep")


def test_dismissal_in_sentence():
    assert _is_dismissal("okay jarvis, that'll be all for tonight")
    assert _is_dismissal("you can stand down now")


def test_dismissal_survives_deepgram_punctuation():
    """Deepgram punctuates: 'Goodbye, Jarvis.' must still dismiss."""
    assert _is_dismissal("Goodbye, Jarvis.")
    assert _is_dismissal("Stand down.")
    assert _is_dismissal("Dismissed.")


@pytest.mark.parametrize("phrase", DISMISSAL_PHRASES)
def test_every_configured_phrase_is_reachable(phrase):
    """Each configured phrase, as Deepgram would transcribe it (sentence-cased,
    trailing period), must trigger dismissal. Catches phrases entered with
    capitals or typos that the matcher can never produce a hit for."""
    spoken = phrase[:1].upper() + phrase[1:].lower() + "."
    assert _is_dismissal(spoken), f"configured phrase {phrase!r} can never match"


def test_non_dismissal():
    assert not _is_dismissal("what's the weather")
    assert not _is_dismissal("tell me the news")
    assert not _is_dismissal("hello jarvis")
    assert not _is_dismissal("")


def test_phrase_must_match_whole_words():
    """Plain substring matching dismissed the session on 'under[stand down]load'."""
    assert not _is_dismissal("I understand, download it for me")
    assert not _is_dismissal("That will be allowed?")
