"""Is this line just small talk? Then JARVIS answers it without his tools and
without the conversation before it (agent_friday._just_this_line).

Both pulled Gemini into a machine's voice on casual lines ("All systems
nominal, sir", "just keeping the electrons flowing"). Measured on the same
casual lines: with the ~70 tool definitions attached about half the replies
slipped, without them about a tenth, and the first word came ~0.3 s sooner.
Over whole casual conversations, one slip got echoed by the next replies:
44% slipped with the history, 13% without it. Temperature barely mattered
once both were gone, and example exchanges made it worse.

Deliberately cautious: a line counts only if it is short, has a clear casual
marker, and has nothing that looks like a request. A miss just takes the
normal path; a false hit would mean a chatty reply instead of an action, so
anything request-like disqualifies it. So does a reaction to what just
happened ("lol", "thanks"): it needs the conversation.

Deliberately free of any MCP/LiveKit import.
"""
from __future__ import annotations

import re

_MAX_WORDS = 8

# Matched as whole words on the normalized line (lowercase, no apostrophes).
_CASUAL = [
    "hi", "hey", "hello", "yo", "sup", "wassup", "whats up", "whats good", "whats new",
    "how are you", "how you doing", "how are you doing", "hows it going", "how is it going",
    "holding up", "hows life", "how have you been",
    "morning", "good morning", "night", "good night", "goodnight", "evening", "good evening",
    "baby", "babe", "bro", "bruh", "dude", "my guy", "my man", "homie", "buddy", "pal",
    "love you", "miss me", "missed you", "miss you",
    "youre the best", "the goat", "legend", "you rock",
    "tired", "exhausted", "sleepy", "bored", "ugh",
]
_REQUEST = [
    "open", "close", "launch", "start", "stop", "play", "pause", "resume", "skip", "next",
    "previous", "turn", "set", "remind", "reminder", "timer", "alarm", "schedule", "cancel",
    "search", "look up", "lookup", "find", "show", "send", "text", "message", "email",
    "emails", "mail", "call", "weather", "forecast", "temperature", "time", "date", "day",
    "today", "tomorrow", "tonight", "news", "calendar", "event", "meeting", "volume", "mute",
    "louder", "quieter", "file", "files", "folder", "delete", "create", "write", "read",
    "copy", "paste", "move", "save", "note", "remember", "forget", "calculate", "percent",
    "convert", "what is", "whats the", "who is", "where", "when", "how much", "how many",
    "how long", "how far", "directions", "song", "music", "spotify", "chrome", "app",
    "screen", "deploy", "build", "code", "robot", "frc", "switch", "restart", "shut down",
    "lock", "wifi", "battery", "clipboard", "can you", "could you", "would you", "please",
    "tell me", "explain", "help",
]


# Reactions to what just happened: they need the conversation small talk goes
# without, so they rule a line out even next to a casual word ("thanks, bro").
_REACTION = ["thanks", "thank you", "thx", "lol", "lmao", "haha", "hahaha", "nice", "good one"]


def _pattern(phrases: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b")


_CASUAL_RE = _pattern(_CASUAL)
_NOT_SMALL_TALK_RE = _pattern(_REQUEST + _REACTION)


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("'", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(word for word in text.split() if word != "jarvis")


def is_small_talk(text: str) -> bool:
    """True for a short casual line ("Sup Jarvis.", "Love you, Jarvis.") with
    nothing in it that looks like a request."""
    line = _normalize(text or "")
    if not line or len(line.split()) > _MAX_WORDS:
        return False
    return bool(_CASUAL_RE.search(line)) and not _NOT_SMALL_TALK_RE.search(line)
