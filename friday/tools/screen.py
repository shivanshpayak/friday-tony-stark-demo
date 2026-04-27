"""Screen capture + Gemini vision — read what's on the user's screen."""
import os
import io
import base64
import asyncio
import logging

import httpx
from PIL import ImageGrab
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("friday-agent")

# Cap longest side before encoding. Keeps payload small for latency without
# hurting text legibility for typical UI screenshots.
_MAX_DIM = 1920

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


def _capture_and_analyze(prompt: str) -> str:
    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception as e:
        return f"Screen capture failed: {e}"

    img.thumbnail((_MAX_DIM, _MAX_DIM))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return "GOOGLE_API_KEY not set."

    instruction = (
        _FOCUSED_INSTRUCTION_TEMPLATE.format(prompt=prompt.strip())
        if prompt.strip()
        else _DEFAULT_INSTRUCTION
    )

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

    try:
        response = httpx.post(url, json=payload, timeout=20.0)
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.warning("Gemini vision call failed: %s", e)
        return f"Vision analysis failed: {e}"


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
