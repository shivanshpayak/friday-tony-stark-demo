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

from friday.screen_scroll import capture_scrolling

logger = logging.getLogger("friday-agent")

# Cap longest side before encoding. Keeps payload small for latency without
# hurting text legibility for typical UI screenshots.
_MAX_DIM = 1920
_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_RETRIES = 2
SCROLL_CLICKS = 10  # wheel notches per scroll step (~one viewport; tuned live)

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


def _encode_image_b64(img) -> str:
    img.thumbnail((_MAX_DIM, _MAX_DIM))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _analyze_images(images: list, instruction: str) -> str:
    """Send one or more images + an instruction to Gemini vision; return the text."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return "GOOGLE_API_KEY not set."

    parts = [{"text": instruction}]
    for img in images:
        parts.append({"inline_data": {"mimeType": "image/png",
                                      "data": _encode_image_b64(img)}})

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {"contents": [{"parts": parts}]}

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = httpx.post(url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            text = _extract_text(response.json())
            if text:
                return text
            last_error = "No text returned by vision model."
        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini vision call failed (attempt %d/%d): %s",
                           attempt, _MAX_RETRIES, e)
    return f"Vision analysis failed: {last_error}"


def _capture_and_analyze(prompt: str) -> str:
    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception as e:
        return f"Screen capture failed: {e}"

    text = _analyze_images([img], _build_instruction(prompt))
    # Keep spoken output compact unless the user explicitly asked to read verbatim.
    if text and not _wants_verbatim_text(prompt):
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _active_window_center():
    """Center (x, y) of the foreground window; raises if it can't be found."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect failed")
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def _make_scroll_fn(pyautogui):
    def scroll():
        try:
            x, y = _active_window_center()
            pyautogui.moveTo(x, y)
        except Exception:
            pass  # fall back to scrolling at the current cursor position
        pyautogui.scroll(-SCROLL_CLICKS)
    return scroll


def _build_scroll_instruction(prompt: str) -> str:
    ask = (prompt or "").strip() or "Describe what this content is and summarize it."
    return (
        "These are sequential top-to-bottom screenshots of a single scrolling view. "
        "Read the content in order and IGNORE repeated or overlapping rows between "
        f"consecutive images. The user asked: {ask} "
        "Answer concisely for spoken delivery; if it's a long list, give counts and "
        "highlights rather than reading every item."
    )


def _capture_scrolling_and_analyze(prompt: str) -> str:
    try:
        import pyautogui
    except Exception:
        return "I need the pyautogui component to read scrolling content, sir."
    try:
        frames = capture_scrolling(
            grab_fn=lambda: ImageGrab.grab(all_screens=True),
            scroll_fn=_make_scroll_fn(pyautogui),
        )
    except Exception as e:
        return f"Screen capture failed: {e}"
    if not frames:
        return "I couldn't capture the screen, sir."
    return _analyze_images(frames, _build_scroll_instruction(prompt))


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

    @mcp.tool()
    async def read_long_content(prompt: str = "") -> str:
        """Read content that scrolls PAST the visible screen — a long list, chat
        log, playlist, or article. FRIDAY scrolls the active window top to bottom,
        capturing as it goes, then answers your prompt over ALL of it (it will NOT
        read a huge list aloud — it summarizes / answers). Slower than read_screen
        (a few seconds) and it visibly scrolls the window, so say a short line
        first (ACTING OUT LOUD). Use for "read this whole list", "how many X are
        here", "is Y in this", "what's in this playlist" — anything longer than one
        screen. For a single visible screen, use read_screen instead."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _capture_scrolling_and_analyze, prompt
        )
