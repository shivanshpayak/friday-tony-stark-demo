"""The alarm — what happens when a timer or clock alarm goes off while JARVIS is idle.

The agent speaks the item's line, then reports SCHEDULED_FIRED to the
launcher. For a fresh timer or an alarm the launcher enters its ALARM state: a
dim chime loops and the mic is listened to for a stop phrase only
(ALARM_STOP_PHRASES, or ALARM_SNOOZE_PHRASES to have it ring again in
ALARM_SNOOZE_MINUTES) — through the fast path's offline Vosk model with a
closed grammar, so nothing else said is taken as input and no STT or LLM call
is made. A stop phrase, push-to-talk, or ALARM_MAX_SECONDS ends it and puts
the overlay away.

Before this, a timer firing while idle spoke through a session nobody had
opened: the overlay was driven to "speaking" by the agent's PROCESSING /
SPEAKING lines and nothing ever put it away again.

Light at import (the launcher loads it at boot): webrtcvad and the fast-path
recognizer are imported only when a listener is built.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import wave
from array import array
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from friday import plat
from friday.config import (
    ALARM_SNOOZE_MINUTES,
    ALARM_SNOOZE_PHRASES,
    ALARM_STALE_SECONDS,
    ALARM_STOP_PHRASES,
    ALARM_VOLUME,
    FASTPATH_VAD_AGGRESSIVENESS,
)

logger = logging.getLogger("friday-launcher")

UNK = "[unk]"


# --- deciding to ring --------------------------------------------------------

def should_ring(kind: str, fire_at: str, now: datetime | None = None) -> bool:
    """True for an alarm, or a timer that has only just come due.

    Reminders are said once. So is a timer that came due while JARVIS was off
    (the scheduler fires it on the first tick after boot): nobody is waiting
    on it any more. An alarm that late never gets this far — the scheduler
    skips it (ALARM_MISSED_SECONDS).
    """
    if kind == "alarm":
        return True
    if kind != "timer":
        return False
    try:
        due = datetime.fromisoformat(fire_at)
    except (TypeError, ValueError):
        return True     # no usable time: it is firing now, so it is fresh
    late = ((now or datetime.now()) - due).total_seconds()
    return late < ALARM_STALE_SECONDS


# --- stop phrases ------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation (keeping apostrophes), collapse whitespace."""
    text = (text or "").lower().replace("’", "'").replace(UNK, " ")
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


STOP_PHRASES: tuple[str, ...] = tuple(dict.fromkeys(
    _normalize(p) for p in ALARM_STOP_PHRASES if _normalize(p)
))

SNOOZE_PHRASES: tuple[str, ...] = tuple(dict.fromkeys(
    _normalize(p) for p in ALARM_SNOOZE_PHRASES if _normalize(p)
))


# Heard but never a stop. A closed grammar forces speech onto its nearest
# phrase, and "goodbye jarvis" makes "jarvis" one of its words: without this,
# 1 of 8 of the user's recorded "hey jarvis" clips decoded as "goodbye jarvis".
DECOY_PHRASES: tuple[str, ...] = ("hey jarvis",)


def grammar() -> list[str]:
    """The only things the stop listener can hear: the phrases, decoys, or [unk]."""
    return [*STOP_PHRASES, *SNOOZE_PHRASES, *DECOY_PHRASES, UNK]


def stop_phrase_in(text: str) -> str | None:
    """The stop or snooze phrase `text` contains as whole words, if any.

    Whole words, so "stop" doesn't hit "stopwatch"; anywhere in the text, so
    "[unk] stop" and "okay thank you" both count. Snooze wins when both are
    there ("okay snooze"): it rings again, where a lost snooze would not.
    """
    padded = f" {_normalize(text)} "
    for phrase in (*SNOOZE_PHRASES, *STOP_PHRASES):
        if f" {phrase} " in padded:
            return phrase
    return None


def is_snooze(phrase: str | None) -> bool:
    return phrase in SNOOZE_PHRASES


def snooze(fired: dict, now: datetime | None = None) -> datetime:
    """Put what was ringing back on the schedule ALARM_SNOOZE_MINUTES from now,
    as a one-time alarm with the same line; return when it rings.

    Labelled "snoozed <label>" so cancelling the repeating alarm by its label
    doesn't hit this one instead.
    """
    from friday.scheduling import ScheduledItem, add   # pydantic: only on a snooze

    now = now or datetime.now()
    at = (now + timedelta(minutes=ALARM_SNOOZE_MINUTES)).replace(microsecond=0)
    add(ScheduledItem(
        kind="alarm",
        fire_at=at.isoformat(),
        message=str(fired.get("message") or "Your alarm, sir."),
        label=f"snoozed {fired.get('label') or ''}".strip(),
        created_at=now.isoformat(),
    ))
    return at


# --- the chime ---------------------------------------------------------------

_RATE = 22050
_NOTES = ((0.00, 880.0), (0.16, 1174.7))   # A5 then D6: a soft two-note bell
_NOTE_SECONDS = 0.9                          # each note's decay tail
_RING_SECONDS = 1.6                          # one ring, rest included
_RINGS_PER_CLIP = 3                          # fewer player launches per minute


def build_clip(path: Path, volume: float = ALARM_VOLUME) -> float:
    """Write the alarm clip to `path`; return its length in seconds.

    Sine bells with a slight overtone and a fast exponential decay, peaking at
    `volume` of full scale. Rebuilt at every launcher boot (~0.1 s) so a
    change to ALARM_VOLUME takes effect without deleting anything.
    """
    total = int(_RATE * _RING_SECONDS * _RINGS_PER_CLIP)
    mix = [0.0] * total
    attack = _RATE * 0.005                   # 5 ms fade-in: no click
    for ring in range(_RINGS_PER_CLIP):
        for offset, freq in _NOTES:
            start = int(_RATE * (ring * _RING_SECONDS + offset))
            for i in range(min(int(_RATE * _NOTE_SECONDS), total - start)):
                t = i / _RATE
                envelope = math.exp(-5.0 * t) * min(1.0, i / attack)
                mix[start + i] += envelope * (
                    math.sin(2 * math.pi * freq * t)
                    + 0.25 * math.sin(4 * math.pi * freq * t)
                )
    peak = max(abs(s) for s in mix) or 1.0
    scale = max(0.0, min(volume, 1.0)) * 32767 / peak
    samples = array("h", (int(s * scale) for s in mix))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_RATE)
        wf.writeframes(samples.tobytes())
    return total / _RATE


class Ringer:
    """Loops the alarm clip on a background thread until stop()."""

    def __init__(self, clip: Path, seconds: float,
                 play: Callable = plat.play_wav, stop: Callable = plat.stop_sound):
        self._clip = clip
        self._seconds = seconds
        self._play = play
        self._stop_sound = stop
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        handle = None
        while not self._stopped.is_set():
            try:
                handle = self._play(self._clip)
            except Exception as e:
                logger.warning("Alarm chime failed (%s) — ringing silently", e)
                return
            self._stopped.wait(self._seconds)
        self._stop_sound(handle)

    def stop(self) -> None:
        """Silence it now, mid-clip included."""
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


# --- listening for the stop --------------------------------------------------

CHUNK_MS = 80.0             # the wake-word stream's chunk (friday_launcher.AUDIO_CHUNK)
PRE_ROLL_CHUNKS = 3         # kept from before speech onset: the "s" of "stop"
                            # often reads as unvoiced and would be clipped
END_SILENCE_MS = 400.0      # silence that ends an utterance
MAX_UTTERANCE_MS = 4000.0   # decode anyway after this much speech (or chime)


class StopListener:
    """Hears only the stop phrases on the wake-word mic stream.

    webrtcvad cuts the stream into utterances; each is decoded by a Vosk
    recognizer whose grammar is grammar(), so anything that isn't a stop
    phrase comes out as [unk] and is dropped.
    """

    def __init__(self, make_recognizer: Callable[[], object]):
        import webrtcvad
        from friday.fastpath.recognizer import TailRecognizer

        self._make_recognizer = make_recognizer
        self._new_vad = lambda: webrtcvad.Vad(FASTPATH_VAD_AGGRESSIVENESS)
        self._is_voiced = TailRecognizer.chunk_is_voiced

    def wait(self, read_chunk: Callable[[], bytes],
             done: Callable[[], bool]) -> str | None:
        """Block until a stop phrase is heard (returned) or done() (None).

        `read_chunk` returns one 80 ms chunk of 16 kHz int16 audio.
        """
        import json

        vad = self._new_vad()
        pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_CHUNKS)
        rec = None
        speech_ms = silence_ms = 0.0
        while not done():
            raw = read_chunk()
            voiced = self._is_voiced(vad, raw)
            if rec is None:
                if not voiced:
                    pre_roll.append(raw)
                    continue
                rec = self._make_recognizer()
                for earlier in pre_roll:
                    rec.AcceptWaveform(earlier)
                pre_roll.clear()
                speech_ms = silence_ms = 0.0
            rec.AcceptWaveform(raw)
            if voiced:
                speech_ms += CHUNK_MS
                silence_ms = 0.0
            else:
                silence_ms += CHUNK_MS
            if silence_ms < END_SILENCE_MS and speech_ms < MAX_UTTERANCE_MS:
                continue
            try:
                text = json.loads(rec.FinalResult()).get("text", "")
            except ValueError:
                text = ""
            rec = None
            phrase = stop_phrase_in(text)
            if text and text != UNK:
                logger.info("Alarm: heard %r → %s", text, phrase or "not a stop phrase")
            if phrase:
                return phrase
        return None
