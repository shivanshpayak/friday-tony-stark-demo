"""The fast-path command table — single source of truth.

Adding a command is one entry in COMMANDS. The Vosk grammar is derived from
this table, so a new phrase becomes recognizable automatically.

Keep phrases SHORT and acoustically distinct. Every phrase here is one the
recognizer is allowed to hear instead of routing to the LLM, so a phrase that
overlaps with normal speech will steal real requests.

Every WORD used here must exist in the Vosk model's vocabulary. Words that
don't are silently dropped from the grammar by Vosk, making the command
unmatchable — "unmute" was exactly this case, which is why the unmute command
is spelled "sound on". TailRecognizer checks this at startup and logs an ERROR
for any out-of-vocabulary word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from friday.fastpath import actions

UNK = "[unk]"

_TRAILING_PUNCT = re.compile(r"[.,!?;:]+$")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FastCommand:
    name: str                   # stable id, used in logs
    phrases: tuple[str, ...]    # lowercase grammar entries
    action: Callable[[], None]  # zero-arg, synchronous


COMMANDS: tuple[FastCommand, ...] = (
    FastCommand("mute",       ("mute", "sound off"),                  actions.mute),
    FastCommand("unmute",     ("sound on", "audio on"),               actions.unmute),
    FastCommand("play_pause", ("play", "pause"),                      actions.play_pause),
    FastCommand("next_track", ("next", "skip", "next track"),         actions.next_track),
    FastCommand("prev_track", ("back", "previous", "previous track"), actions.previous_track),
    FastCommand("louder",     ("louder", "volume up"),                actions.louder),
    FastCommand("quieter",    ("quieter", "volume down"),             actions.quieter),
    FastCommand("stop",       ("stop",),                              actions.stop_media),
)


def normalize(text: str) -> str:
    """Lowercase, strip, drop trailing punctuation, collapse inner whitespace."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", _TRAILING_PUNCT.sub("", text.lower().strip()))


_INDEX: dict[str, FastCommand] = {
    normalize(phrase): cmd for cmd in COMMANDS for phrase in cmd.phrases
}


def grammar_phrases() -> list[str]:
    """Every phrase plus [unk], de-duplicated, in declaration order.

    Handed to KaldiRecognizer, which then can only emit one of these.
    """
    phrases = list(dict.fromkeys(
        normalize(p) for cmd in COMMANDS for p in cmd.phrases
    ))
    return phrases + [UNK]


def grammar_words() -> set[str]:
    """Every distinct word across all phrases. Used by the vocabulary guard."""
    return {word for phrase in _INDEX for word in phrase.split()}


def command_for_word(word: str) -> str | None:
    """Name of the first command whose phrases contain `word`, for error text."""
    for cmd in COMMANDS:
        for phrase in cmd.phrases:
            if word in normalize(phrase).split():
                return cmd.name
    return None


def lookup(text: str) -> FastCommand | None:
    """Resolve recognizer output to a command, or None if it isn't one."""
    n = normalize(text)
    if not n or n == UNK:
        return None
    return _INDEX.get(n)
