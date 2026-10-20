"""Which song is it? From a few lyrics he sang or half-remembers.

Spotify search doesn't look at lyrics, so "the song that goes like it's so
electric baby when I turn it on" went straight into it and came back with the
wrong song, nine times in a row. Gemini with Google Search names the song even
from words the speech recognizer got wrong. Measured on the lyrics from the
logs: gemini-3.5-flash with search got 5 of 5 in ~2-3s; without search it got
4 of 5 in ~1s, so that is the fallback when search gives no answer.

Deliberately free of any MCP/LiveKit import.
"""
from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger("friday-agent")

MODEL = "gemini-3.5-flash"
_TIMEOUT = 15.0
_PROMPT = (
    "Someone is trying to name a song by singing a few of its lyrics from memory. "
    "Speech recognition transcribed them, so words may be misheard or slightly off. "
    'Lyrics: "{lyrics}"\n'
    "Which song is it? Reply with exactly one line: Title — Artist. "
    "If you can't tell, reply: unknown"
)


def parse_answer(text: str) -> tuple[str, str] | None:
    """("Title", "Artist") from a "Title — Artist" line, or None."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = line.strip().strip("*").strip()
    if not line or line.lower().startswith("unknown"):
        return None
    parts = re.split(r"\s+[—–]\s+", line, maxsplit=1)
    if len(parts) != 2:
        parts = line.rsplit(" - ", 1)
    if len(parts) != 2:
        return None
    title, artist = (p.strip().strip('"“”*').strip() for p in parts)
    return (title, artist) if title and artist else None


def _ask(lyrics: str, *, search: bool) -> str:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set")
    body = {
        "contents": [{"parts": [{"text": _PROMPT.format(lyrics=lyrics)}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
    }
    if search:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    # In a header, not the URL: httpx logs request URLs into logs/friday.log.
    response = httpx.post(url, json=body, headers={"x-goog-api-key": api_key},
                          timeout=_TIMEOUT)
    response.raise_for_status()
    for candidate in response.json().get("candidates") or []:
        parts = (candidate.get("content") or {}).get("parts") or []
        text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if text:
            return text
    return ""


def identify(lyrics: str) -> tuple[str, str] | None:
    """(title, artist) of the song these lyrics come from, or None."""
    for search in (True, False):
        try:
            found = parse_answer(_ask(lyrics, search=search))
        except Exception as e:
            logger.warning("Lyrics lookup failed (search=%s): %s", search, e)
            continue
        if found:
            return found
    return None
