"""Small talk is answered without tools; anything that might be a request never is."""
import pytest

from friday.small_talk import is_small_talk

CASUAL = [
    "How you doing, baby?", "Love you, Jarvis.", "What's good, bro?",
    "How are you holding up, my guy?", "Morning, babe. Miss me?",
    "You're the goat, dude.", "Ugh, I'm so tired.", "Sup Jarvis.", "Night, babe.",
    "Hey.",
]

REQUESTS = [
    "Open Spotify.", "Set a timer for five minutes.", "What's the weather like?",
    "Pause the music.", "Skip this song.", "Remind me to call mom at 6 pm.",
    "Turn the volume down a bit.", "Search the web for the latest SpaceX launch.",
    "What's on my calendar today?", "Close Chrome.", "What's 17 percent of 240?",
    "Play Bohemian Rhapsody on Spotify.", "Any new emails?",
    # casual words wrapped around a request
    "Yo, open Spotify.", "Hey, what's the weather?", "Bro, set a timer.",
    "Babe, remind me tomorrow.", "Thanks, now close Chrome.", "Hey Jarvis, what time is it?",
    "Dude, can you help me with my code?", "Morning. What's on today?",
]


@pytest.mark.parametrize("line", CASUAL)
def test_small_talk(line):
    assert is_small_talk(line)


@pytest.mark.parametrize("line", REQUESTS)
def test_requests_keep_their_tools(line):
    assert not is_small_talk(line)


@pytest.mark.parametrize("line", ["lol", "Haha.", "Thanks, Jarvis.", "Thank you.",
                                  "Thanks, bro.", "lol dude", "Nice one, babe."])
def test_reactions_keep_the_conversation(line):
    # A reaction to what just happened needs the history small talk goes without.
    assert not is_small_talk(line)


def test_long_lines_are_not_small_talk():
    assert not is_small_talk("Hey bro, so I was thinking about the whole robot arm situation again")


def test_empty_is_not_small_talk():
    assert not is_small_talk("")
    assert not is_small_talk("Jarvis")
