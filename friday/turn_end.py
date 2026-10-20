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

Deepgram also goes back over audio it already reported, up to ~8s later and
often with a word more or less at the front: "Set a timer for thirty
minutes." came back as "are of Set a timer for thirty minutes." and then
twice more, and the second copy set a second timer. Each word carries its
audio time, so words for audio already passed on are cut (_unheard).
"""

import asyncio
import dataclasses
import logging
import time
from typing import AsyncIterable, AsyncIterator

from livekit.agents import stt

logger = logging.getLogger("friday-agent")

_START = stt.SpeechEventType.START_OF_SPEECH
_END = stt.SpeechEventType.END_OF_SPEECH
_FINAL = stt.SpeechEventType.FINAL_TRANSCRIPT
_INTERIM = stt.SpeechEventType.INTERIM_TRANSCRIPT

# How long words passed on are remembered for spotting a copy. The latest copy
# in the logs came 8s after the original; a real repeat comes after a reply.
_MEMORY = 15.0

# A turn that stops on one of these isn't finished; he is thinking of the
# rest. In the logs a lone "Play" (pause) toggled the music three times, and
# "Keep skipping ... until you get to ... attractive by Jordan" went in as
# three requests. Words that often do end a sentence ("on", "is", "her",
# "call") are left out: waiting on them would slow down finished requests.
_UNFINISHED = frozenset("""
    a an the my your his our their some
    to by of for from with into like until than
    and but or because if
    play open set put pull queue send
    it's i'm i'll i'd gonna wanna called named
    i let let's uh um er hmm
""".split())


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


def _timed(ev) -> list | None:
    """The event's words with their audio times, or None if the STT gave none."""
    words = ev.alternatives[0].words if ev.alternatives else None
    if not words or not all(isinstance(getattr(w, "start_time", None), (int, float))
                            and isinstance(getattr(w, "end_time", None), (int, float))
                            for w in words):
        return None
    return words


class _Heard:
    """The timed words passed on as final in the last _MEMORY seconds."""

    def __init__(self) -> None:
        self._spans: list[tuple[float, float, set[str]]] = []  # (when, audio end, words)

    def add(self, words: list) -> None:
        self._spans.append((time.monotonic(), max(w.end_time for w in words),
                            {_bare(w) for w in words}))

    def recent(self) -> tuple[float, set[str]]:
        """(audio time up to which words were heard, those words)."""
        cutoff = time.monotonic() - _MEMORY
        self._spans = [s for s in self._spans if s[0] >= cutoff]
        if not self._spans:
            return 0.0, set()
        return (max(end for _, end, _ in self._spans),
                set().union(*(words for _, _, words in self._spans)))


def _bare(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum() or c == "'")


def _unheard(ev, words: list, heard: _Heard):
    """`ev` minus its leading words for audio already passed on; None if no
    word is new.

    Audio times alone can mislead: a reconnected Deepgram stream starts its
    clock again at zero, so new speech would look old. A copy also repeats the
    words, so the cut only happens when most of the words in that stretch were
    just heard.
    """
    until, recent = heard.recent()
    cut = next((i for i, w in enumerate(words)
                if (w.start_time + w.end_time) / 2 > until), len(words))
    if cut == 0 or sum(_bare(w) in recent for w in words[:cut]) * 2 < cut:
        return ev
    if cut == len(words):
        return None
    tokens = ev.alternatives[0].text.split()
    rest = tokens[cut:] if len(tokens) == len(words) else [str(w) for w in words[cut:]]
    first = dataclasses.replace(ev.alternatives[0], text=" ".join(rest),
                                words=words[cut:], start_time=words[cut].start_time)
    return dataclasses.replace(ev, alternatives=[first, *ev.alternatives[1:]])


async def end_turn_on_words(events: AsyncIterable, quiet: float,
                            unfinished_quiet: float | None = None) -> AsyncIterator:
    """Re-time START/END_OF_SPEECH in an STT event stream (see module doc).

    The turn ends `quiet` seconds after the words stop changing, or at
    Deepgram's endpoint. When the last word leaves the sentence hanging
    (_UNFINISHED), it waits `unfinished_quiet` seconds instead, and holds
    Deepgram's endpoint until then too.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    hold = max(quiet, unfinished_quiet or quiet)

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
    promoted: list[str] = []    # untimed words this wrapper finalised itself
    heard = _Heard()            # timed words passed on as final
    tail: list[str] = []        # words of this turn's latest transcript
    try:
        while True:
            unfinished = bool(tail) and tail[-1] in _UNFINISHED
            limit = hold if unfinished else quiet
            try:
                wait = max(0.0, changed_at + limit - time.monotonic()) if talking else None
                ev = await asyncio.wait_for(queue.get(), wait)
            except asyncio.TimeoutError:
                if interim is not None:
                    timed = _timed(interim)
                    if timed:
                        heard.add(timed)
                    else:
                        # Every stretch promoted since Deepgram's last final:
                        # its late final reports them all, from the first.
                        promoted += _words(interim)
                    yield _with_text(interim, _FINAL, interim.alternatives[0].text)
                talking, interim, tail = False, None, []
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
                if talking and unfinished and hold > quiet:
                    continue            # the timer above ends it if nothing follows
                if talking:
                    talking, interim, tail = False, None, []
                    yield ev
                continue                # else: this turn was already ended here
            if ev.type not in (_FINAL, _INTERIM) or not _words(ev):
                yield ev
                continue

            timed = _timed(ev)
            if timed:
                fresh = _unheard(ev, timed, heard)
                if fresh is not ev:
                    logger.info("STT went back over words already handled: %r -> %r",
                                ev.alternatives[0].text,
                                fresh.alternatives[0].text if fresh else None)
                if fresh is None:
                    continue
                ev = fresh
            elif promoted:
                fresh = _after(ev, promoted)
                if ev.type == _FINAL:
                    promoted = []       # that stretch of audio is now closed
                if fresh is None:
                    continue
                ev = fresh
            if not talking:
                talking = True
                yield stt.SpeechEvent(type=_START)
            elif unfinished and time.monotonic() - changed_at > quiet:
                logger.info("Waited past %r and he went on: %r", tail[-1],
                            ev.alternatives[0].text)
            if ev.type == _FINAL:
                if timed:
                    heard.add(ev.alternatives[0].words)
                interim, changed_at = None, time.monotonic()
            elif interim is None or _words(ev) != _words(interim):
                interim, changed_at = ev, time.monotonic()
            tail = _words(ev)
            yield ev
    finally:
        pump_task.cancel()
