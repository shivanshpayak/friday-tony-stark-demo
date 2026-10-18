"""
Decide when the user started and stopped talking from WORDS, not from sound.

Deepgram ends a turn (`speech_final`) after 300 ms of silence, and announces
speech (`SpeechStarted`) from its own voice-activity detector. Both listen to
the audio, and a laptop fan is steady noise that is never silent:

  * the silence never comes, so the turn never ends — JARVIS sat on a finished
    request until the mic was muted, which finally gave it real silence;
  * and a "speech started" fired by the fan just after a turn did end makes
    livekit cancel the reply it was about to give.

What the fan cannot do is produce words. So here the start of speech is the
first transcript with text in it, and the end of speech is whichever comes
first: Deepgram's own endpoint (still the fast path in a quiet room), or the
transcript going `quiet` seconds without changing.

Under noise Deepgram also holds back the *final* transcript for several
seconds (measured: 3s after the last word, with the endpoint never arriving),
so waiting for it would keep most of the delay. When the words stop changing
while still interim, the interim transcript is promoted to final here, and
Deepgram's late final for the same words is dropped when it turns up.
"""

import asyncio
import dataclasses
import time
from typing import AsyncIterable, AsyncIterator

from livekit.agents import stt

_START = stt.SpeechEventType.START_OF_SPEECH
_END = stt.SpeechEventType.END_OF_SPEECH
_FINAL = stt.SpeechEventType.FINAL_TRANSCRIPT
_INTERIM = stt.SpeechEventType.INTERIM_TRANSCRIPT


def _words(ev) -> list[str]:
    """The event's transcript as bare lowercase words."""
    if not ev.alternatives:
        return []
    text = ev.alternatives[0].text.lower()
    return "".join(c if c.isalnum() or c == "'" else " " for c in text).split()


def _with_text(ev, kind, text: str):
    """A copy of transcript event `ev` with a different type and text."""
    first = dataclasses.replace(ev.alternatives[0], text=text)
    return dataclasses.replace(ev, type=kind, alternatives=[first, *ev.alternatives[1:]])


def _after(ev, spoken: list[str]):
    """`ev` minus the words in `spoken` it repeats; None if nothing is new.

    After an interim transcript was promoted, Deepgram keeps reporting the same
    stretch of audio — the same words, or those words plus whatever was said
    next. Anything that does not begin with them is new speech, passed whole.
    """
    words = _words(ev)
    if words[:len(spoken)] != spoken:
        return ev
    if len(words) == len(spoken):
        return None
    remainder = ev.alternatives[0].text.split()[len(spoken):]
    return _with_text(ev, ev.type, " ".join(remainder))


async def end_turn_on_words(events: AsyncIterable, quiet: float) -> AsyncIterator:
    """Re-time START/END_OF_SPEECH in an STT event stream (see module doc)."""
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump() -> None:
        try:
            async for ev in events:
                queue.put_nowait(ev)
        except Exception as e:          # re-raised in the consumer below
            queue.put_nowait(e)
        finally:
            queue.put_nowait(done)

    pump_task = asyncio.create_task(pump())
    talking = False             # START_OF_SPEECH sent, END_OF_SPEECH not yet
    changed_at = 0.0            # when the transcript last gained or changed words
    interim = None              # latest interim transcript not yet finalised
    promoted: list[str] = []    # words of an interim this wrapper finalised itself
    try:
        while True:
            try:
                wait = max(0.0, changed_at + quiet - time.monotonic()) if talking else None
                ev = await asyncio.wait_for(queue.get(), wait)
            except asyncio.TimeoutError:
                if interim is not None:
                    promoted = _words(interim)
                    yield _with_text(interim, _FINAL, interim.alternatives[0].text)
                talking, interim = False, None
                yield stt.SpeechEvent(type=_END)
                continue
            if ev is done:
                return
            if isinstance(ev, Exception):
                raise ev
            if not isinstance(ev, stt.SpeechEvent):
                yield ev
                continue

            if ev.type == _START:
                continue                # the detector's opinion; words decide
            if ev.type == _END:
                if talking:
                    talking, interim = False, None
                    yield ev
                continue                # else: this turn was already ended here
            if ev.type not in (_FINAL, _INTERIM) or not _words(ev):
                yield ev
                continue

            if promoted:
                fresh = _after(ev, promoted)
                if ev.type == _FINAL:
                    promoted = []       # that stretch of audio is now closed
                if fresh is None:
                    continue
                ev = fresh
            if not talking:
                talking = True
                yield stt.SpeechEvent(type=_START)
            if ev.type == _FINAL:
                interim, changed_at = None, time.monotonic()
            elif interim is None or _words(ev) != _words(interim):
                interim, changed_at = ev, time.monotonic()
            yield ev
    finally:
        pump_task.cancel()
