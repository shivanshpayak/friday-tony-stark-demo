"""Screen capture + Gemini vision — read what's on the user's screen."""
import os
import io
import base64
import asyncio
import logging
import re

import httpx
from PIL import ImageGrab
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("friday-agent")

# Cap longest side before encoding. Keeps payload small for latency without
# hurting text legibility for typical UI screenshots.
_MAX_DIM = 1920
_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_RETRIES = 2

_DEFAULT_INSTRUCTION = (
    "Look at this screenshot of the user's screen and briefly describe what's "
    "shown — what app is open, what the user appears to be doing, anything "
    "notable. Reply in 1-2 short sentences suitable for spoken delivery."
)

_FOCUSED_INSTRUCTION_TEMPLATE = (
    "Look at this screenshot of the user's screen. The user asked: {prompt}. "
    "Answer concisely in 1-2 sentences suitable for spoken delivery. If they "
    "asked you to read text verbatim, do so without paraphrasing."
)


def _wants_verbatim_text(prompt: str) -> bool:
    p = (prompt or "").lower()
    hints = (
        "read this",
        "read that",
        "verbatim",
        "exact text",
        "word for word",
        "what does this say",
        "ocr",
    )
    return any(h in p for h in hints)


def _build_instruction(prompt: str) -> str:
    cleaned = (prompt or "").strip()
    if not cleaned:
        return _DEFAULT_INSTRUCTION
    if _wants_verbatim_text(cleaned):
        return (
            f"Look at this screenshot. The user asked: {cleaned}. "
            "Extract the on-screen text exactly where possible. "
            "If text is unclear, say which part is unreadable instead of guessing. "
            "Keep it concise."
        )
    return _FOCUSED_INSTRUCTION_TEMPLATE.format(prompt=cleaned)


def _extract_text(response_json: dict) -> str:
    candidates = response_json.get("candidates") or []
    for candidate in candidates:
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text_parts = [p.get("text", "").strip() for p in parts if isinstance(p, dict)]
        text = " ".join([t for t in text_parts if t]).strip()
        if text:
            return text
    return ""


def _capture_and_analyze(prompt: str) -> str:
    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception as e:
        return f"Screen capture failed: {e}"

    img.thumbnail((_MAX_DIM, _MAX_DIM))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return "GOOGLE_API_KEY not set."

    instruction = _build_instruction(prompt)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": instruction},
                {"inline_data": {"mimeType": "image/png", "data": b64_data}},
            ]
        }]
    }

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = httpx.post(url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            text = _extract_text(response.json())
            if text:
                # Keep spoken output compact unless user explicitly asked to read.
                if not _wants_verbatim_text(prompt):
                    text = re.sub(r"\s+", " ", text).strip()
                return text
            last_error = "No text returned by vision model."
        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini vision call failed (attempt %d/%d): %s", attempt, _MAX_RETRIES, e)

    return f"Vision analysis failed: {last_error}"


def register(mcp: FastMCP):

    @mcp.tool(name="read_screen")
    async def read_screen(prompt: str = "") -> str:
        """Capture the user's screen and analyze it with vision AI.
        Use when the user asks "what's on my screen", "read that error",
        "what does this say", "summarize this article", or otherwise refers
        to something visible on their display. Optional `prompt` lets you
        focus the analysis (e.g. "read the error message verbatim",
        "what's the title of this article")."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _capture_and_analyze, prompt
        )
