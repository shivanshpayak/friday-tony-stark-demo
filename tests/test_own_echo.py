"""Jarvis hearing the tail of his own "sir" through the mic and answering it,
every few seconds ("Sir?" -> "Yes, sir?" -> "Sir?"...). The real filter in
agent_friday, against real-looking Deepgram events."""
import asyncio

from livekit.agents import stt

from agent_friday import _drop_own_echo, _is_own_echo


def test_only_sir_is_an_echo():
    assert _is_own_echo("Sir?")
    assert _is_own_echo("Sir? Sir? Sir?")
    assert _is_own_echo(" sir. ")


def test_anything_he_actually_says_is_not():
    assert not _is_own_echo("Yes.")
    assert not _is_own_echo("Sure.")
    assert not _is_own_echo("Yes, sir.")
    assert not _is_own_echo("Sir, open Chrome")
    assert not _is_own_echo("")
    assert not _is_own_echo("Sirius")


def _event(kind, text):
    return stt.SpeechEvent(type=kind, alternatives=[stt.SpeechData(language="en", text=text)])


def test_the_echo_never_reaches_the_turn_logic():
    events = [
        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH),
        _event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "Sir"),
        _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Sir?"),
        _event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "Put in the"),
        _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Put in the answer."),
    ]

    async def source():
        for ev in events:
            yield ev

    async def collect():
        return [ev async for ev in _drop_own_echo(source())]

    kept = asyncio.run(collect())
    texts = [ev.alternatives[0].text for ev in kept if ev.alternatives]
    assert texts == ["Put in the", "Put in the answer."]
    assert kept[0].type == stt.SpeechEventType.START_OF_SPEECH
