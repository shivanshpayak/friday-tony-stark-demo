# Screen Reach & Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give FRIDAY (A) an `open_on_screen` tool to pull up a URL / Google search / local file, and (B) a `read_long_content` tool that scrolls the active window and reads content that runs past the viewport via multi-image vision.

**Architecture:** A is a thin tool in `friday/tools/web.py` reusing files.py's root check. B splits into a pure, injectable scroll-capture engine (`friday/screen_scroll.py`, unit-tested with fakes) and a tool in `friday/tools/screen.py` that wires real `pyautogui` scrolling + `ImageGrab` capture into a generalized multi-image Gemini vision call.

**Tech Stack:** Python, pytest, `webbrowser`/`os.startfile`, `PIL.ImageGrab`, Gemini vision (httpx), `pyautogui` (new), `ctypes` (active-window). Spec: [docs/superpowers/specs/2026-07-13-screen-reach-and-read-design.md](../specs/2026-07-13-screen-reach-and-read-design.md).

**Note on git:** Per the owner's standing preference, this plan has **no commit steps**. Leave everything uncommitted; do not run git.

**Run tests with:** `uv run pytest <path> -v`

---

### Task 1: Feature A — `open_on_screen` (`friday/tools/web.py`)

**Files:**
- Modify: `friday/tools/web.py` (add a module-level impl + a registered tool)
- Test: `tests/test_open_on_screen.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_open_on_screen.py`:

```python
"""Tests for open_on_screen kind-detection and routing."""
import os
import webbrowser

import friday.tools.files as files_mod
from friday.tools.web import open_on_screen_impl


def test_auto_opens_bare_domain_as_url(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    msg = open_on_screen_impl("github.com", "auto")
    assert opened["url"] == "https://github.com"
    assert "sir" in msg.lower()


def test_auto_falls_back_to_google_search(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    open_on_screen_impl("weather in tokyo", "auto")
    assert opened["url"].startswith("https://www.google.com/search?q=")
    assert "weather" in opened["url"]


def test_explicit_search_kind(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    open_on_screen_impl("late 90s trip hop", "search")
    assert opened["url"].startswith("https://www.google.com/search?q=")


def test_file_kind_opens_path_in_roots(monkeypatch, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hi")
    started = {}
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: f)
    monkeypatch.setattr(os, "startfile", lambda p: started.setdefault("p", p), raising=False)
    msg = open_on_screen_impl(str(f), "file")
    assert started["p"] == str(f)
    assert "sir" in msg.lower()


def test_file_kind_refuses_outside_roots(monkeypatch):
    started = {}
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    monkeypatch.setattr(os, "startfile", lambda p: started.setdefault("p", p), raising=False)
    msg = open_on_screen_impl("C:/Windows/System32", "file")
    assert "outside" in msg.lower()
    assert "p" not in started  # never launched
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_open_on_screen.py -v`
Expected: FAIL — `ImportError: cannot import name 'open_on_screen_impl' from 'friday.tools.web'`.

- [ ] **Step 3: Add the impl + tool to `web.py`**

`friday/tools/web.py` already has `import re` at the top. Add this **module-level**
function (above `def register(mcp):`):

```python
def open_on_screen_impl(target: str, kind: str = "auto") -> str:
    """Pull up a URL, a Google search, or a local file/folder. See the tool
    docstring for arg meaning. Kept module-level so it is unit-testable."""
    import os
    import webbrowser
    from urllib.parse import quote_plus, urlparse
    from friday.tools.files import _resolve_and_check

    target = (target or "").strip()
    if not target:
        return "There's nothing to open, sir."

    def _looks_like_url(t: str) -> bool:
        if urlparse(t).scheme in ("http", "https"):
            return True
        return bool(re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", t))

    def _open_url(t: str) -> str:
        if urlparse(t).scheme not in ("http", "https"):
            t = "https://" + t
        webbrowser.open(t)
        return "Pulling that up, sir."

    def _open_search(t: str) -> str:
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(t))
        return "Pulling that up, sir."

    def _open_file(t: str) -> str:
        path = _resolve_and_check(t)
        if path is None or not path.exists():
            return "That's outside the folders I can open, sir."
        os.startfile(str(path))
        return "Pulling that up, sir."

    kind = (kind or "auto").lower().strip()
    if kind == "url":
        return _open_url(target)
    if kind == "search":
        return _open_search(target)
    if kind == "file":
        return _open_file(target)
    # auto
    if _looks_like_url(target):
        return _open_url(target)
    path = _resolve_and_check(target)
    if path is not None and path.exists():
        return _open_file(target)
    return _open_search(target)
```

Then register the tool **inside** `register(mcp)` (alongside the other `@mcp.tool()`s):

```python
    @mcp.tool()
    def open_on_screen(target: str, kind: str = "auto") -> str:
        """Open/"pull up" something in front of the user: a web URL, a Google
        search, or a local file/folder. Use for "pull up X", "open X", "show me X
        on screen", "google X and open it". First say a short line (ACTING OUT
        LOUD), then call this.

        kind: "auto" (default), "url", "search", or "file".
        - url: opens the target URL in the default browser.
        - search: opens a Google search for the target text.
        - file: opens a local file (default app) or folder (Explorer), restricted
          to Friday's allowed folders.
        - auto: url if it looks like a link, an allowed file if it resolves to one,
          otherwise a Google search.
        """
        return open_on_screen_impl(target, kind)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_open_on_screen.py -v`
Expected: PASS — all 5 tests green.

---

### Task 2: Feature B engine — `friday/screen_scroll.py`

**Files:**
- Create: `friday/screen_scroll.py`
- Test: `tests/test_screen_scroll.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_scroll.py`:

```python
"""Tests for the pure scroll-capture engine."""
from PIL import Image

from friday.screen_scroll import average_hash, hamming, capture_scrolling


def _half(orientation):
    """8x8 grayscale split half black / half white."""
    img = Image.new("L", (8, 8), 0)
    px = img.load()
    for y in range(8):
        for x in range(8):
            edge = x if orientation == "v" else y
            if edge >= 4:
                px[x, y] = 255
    return img


def test_average_hash_distinguishes_images():
    a = _half("v")
    b = _half("h")
    assert hamming(average_hash(a), average_hash(a)) == 0
    assert hamming(average_hash(a), average_hash(b)) > 0


def test_capture_stops_when_stable():
    seq = iter([10, 11, 12, 12, 12])  # frame after 12 repeats -> reached bottom
    grab = lambda: next(seq)
    frames = capture_scrolling(
        grab, lambda: None, max_scrolls=10,
        settle_sleep=lambda s: None, hash_fn=lambda x: x, stable_distance=0,
    )
    assert frames == [10, 11, 12]


def test_capture_hits_max_scrolls():
    c = {"n": 0}
    def grab():
        c["n"] += 1
        return c["n"]  # always different -> never stabilizes
    frames = capture_scrolling(
        grab, lambda: None, max_scrolls=5,
        settle_sleep=lambda s: None, hash_fn=lambda x: x, stable_distance=0,
    )
    assert len(frames) == 6  # initial frame + 5 scrolls
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_screen_scroll.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.screen_scroll'`.

- [ ] **Step 3: Implement the engine**

Create `friday/screen_scroll.py`:

```python
"""Pure scroll-capture engine for reading content that runs past the viewport.

No real screen, no real time, no pyautogui — grab/scroll/sleep are injected so the
capture loop is unit-testable. The tool layer (friday/tools/screen.py) supplies the
real ImageGrab + pyautogui implementations.
"""
from __future__ import annotations

import time
from typing import Any, Callable, List


def average_hash(image) -> int:
    """64-bit average hash: resize to 8x8 grayscale, set each bit where the pixel
    is >= the mean. Uniform images collapse to all-ones (fine — the capture loop
    treats 'no change' as 'reached bottom')."""
    small = image.convert("L").resize((8, 8))
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= (1 << i)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def capture_scrolling(
    grab_fn: Callable[[], Any],
    scroll_fn: Callable[[], None],
    *,
    max_scrolls: int = 12,
    settle_sleep: Callable[[float], None] = time.sleep,
    settle_seconds: float = 0.4,
    hash_fn: Callable[[Any], int] = average_hash,
    stable_distance: int = 2,
) -> List[Any]:
    """Grab -> scroll -> grab, collecting frames until two consecutive frames are
    ~identical (bottom reached) or max_scrolls is hit. Returns the frames."""
    frames = [grab_fn()]
    prev_hash = hash_fn(frames[0])
    for _ in range(max_scrolls):
        scroll_fn()
        settle_sleep(settle_seconds)
        cur = grab_fn()
        cur_hash = hash_fn(cur)
        if hamming(prev_hash, cur_hash) <= stable_distance:
            break  # no change => reached the bottom
        frames.append(cur)
        prev_hash = cur_hash
    return frames
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_screen_scroll.py -v`
Expected: PASS — all 3 tests green.

---

### Task 3: Feature B tool — pyautogui dep + `screen.py` refactor + `read_long_content`

**Files:**
- Modify: `pyproject.toml` (via `uv add pyautogui`)
- Modify: `friday/tools/screen.py`

- [ ] **Step 1: Add the dependency**

Run: `uv add pyautogui`
Expected: `pyproject.toml` gains `pyautogui` and it installs without error.

- [ ] **Step 2: Refactor the vision call to accept multiple images**

In `friday/tools/screen.py`, add these module-level helpers (near
`_capture_and_analyze`). `io` and `base64` are already imported at the top.

```python
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
```

- [ ] **Step 3: Route the existing single-image path through `_analyze_images`**

Replace the body of `_capture_and_analyze` with the behavior-preserving version:

```python
def _capture_and_analyze(prompt: str) -> str:
    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception as e:
        return f"Screen capture failed: {e}"

    text = _analyze_images([img], _build_instruction(prompt))
    if text and not _wants_verbatim_text(prompt):
        text = re.sub(r"\s+", " ", text).strip()
    return text
```

- [ ] **Step 4: Add the scroll helpers + scrolling analyzer**

Add these module-level definitions to `screen.py` (top-level `import` of the engine
goes with the other imports):

```python
from friday.screen_scroll import capture_scrolling

SCROLL_CLICKS = 10  # wheel notches per scroll step (~one viewport; tuned live)


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
```

- [ ] **Step 5: Register the `read_long_content` tool**

Inside `register(mcp)` in `screen.py`, add alongside `read_screen`:

```python
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
```

- [ ] **Step 6: Verify screen.py imports, both screen tools register, and the instruction carries the prompt**

Run:
`uv run python -c "from friday.tools.screen import _build_scroll_instruction, _capture_and_analyze; assert 'find the total' in _build_scroll_instruction('find the total'); from mcp.server.fastmcp import FastMCP; from friday.tools import register_all_tools; import asyncio; m=FastMCP('t'); register_all_tools(m,['core']); names=[t.name for t in asyncio.run(m.list_tools())]; assert 'read_long_content' in names and 'read_screen' in names and 'open_on_screen' in names; print('ok:', [n for n in names if n in ('read_long_content','read_screen','open_on_screen')])"`
Expected: prints `ok: [...]` containing all three tool names — no import error.

- [ ] **Step 7: Run the full test suite (no regressions)**

Run: `uv run pytest -v`
Expected: PASS — all prior tests plus `test_open_on_screen.py` (5) and `test_screen_scroll.py` (3).

---

### Task 4: Documentation

**Files:**
- Modify: `ARCHITECTURE.md` (§7 Feature map / §8 + "Where things live" index)

- [ ] **Step 1: Add the two capabilities to the feature map / background layers**

In [ARCHITECTURE.md](../../../ARCHITECTURE.md), add near the screen/tools description
(§7 Feature map, or a short note in §8):

```markdown
**Screen reach & read:** `open_on_screen` ([`friday/tools/web.py`](friday/tools/web.py))
pulls up a URL, a Google search, or a local file/folder (files bounded to
`FRIDAY_FILE_ROOTS`). `read_long_content` ([`friday/tools/screen.py`](friday/tools/screen.py))
reads content past the viewport: it scrolls the active window (`pyautogui`) and
captures frames until they stop changing (`friday/screen_scroll.py`, capped at 12
scrolls), then sends the sequence to Gemini vision as one multi-image call and
answers over all of it. `read_screen` remains the single-visible-screen path.
```

- [ ] **Step 2: Add the index row**

In the "Where things live" table, add:

```markdown
| Scroll-capture engine (read past the viewport) | `friday/screen_scroll.py` + `read_long_content` in `friday/tools/screen.py` |
```

- [ ] **Step 3: Confirm docs render** — visual check, no command.

---

### Task 5: Live verification (user runs)

**Files:** none (manual runtime verification).

- [ ] **Step 1: Launch console mode** — `uv run friday_voice`

- [ ] **Step 2: Feature A**
  - "pull up the weather in Tokyo" → a Google results page opens in the browser.
  - "open my downloads folder" → Explorer opens Downloads.
  - "open github.com" → the site opens.

- [ ] **Step 3: Feature B (the main event)**
  - Open a long Spotify playlist (longer than one screen), then: "how many songs
    are in this?" / "is <track> in this list?" → FRIDAY says a pre-line, visibly
    scrolls the window top to bottom, then answers from content that was **below**
    the initial viewport.
  - A short window (nothing to scroll) → quick answer, few frames, no endless
    scrolling.

- [ ] **Step 4: Tune if needed**
  - If it scrolls past content too fast (misses rows) or too slow (many redundant
    frames), adjust `SCROLL_CLICKS` in `screen.py`. If it stops early on animated
    content, raise `stable_distance` in the `capture_scrolling` call. If it never
    stops on a truly static long page, that's the `max_scrolls=12` cap doing its
    job — raise it if you have very long content.

- [ ] **Step 5: Record outcome** — if A and B both work, hand back to the owner
  (they handle git). Note any tuning applied.

---

## Self-Review

- **Spec coverage:** Feature A `open_on_screen` (url/search/file/auto + root safety) → Task 1. Feature B pure engine (`average_hash`/`hamming`/`capture_scrolling`) → Task 2. Feature B tool (pyautogui dep, `_analyze_images` refactor preserving `read_screen`, active-window scroll, `read_long_content`) → Task 3. Docs → Task 4. Live verification (incl. tuning knobs `SCROLL_CLICKS`/`max_scrolls`/`stable_distance` from the spec) → Task 5.
- **Placeholder scan:** every code step is complete; every test/verify step has an exact command + expected result. `SCROLL_CLICKS=10` and `max_scrolls=12` are concrete constants, not placeholders. No TBD/TODO.
- **Type/name consistency:** `capture_scrolling(grab_fn, scroll_fn, *, max_scrolls, settle_sleep, settle_seconds, hash_fn, stable_distance)` is defined in Task 2 and called in Task 3 with matching kwargs; its default `hash_fn=average_hash` and `average_hash`/`hamming` are defined in the same module. `_analyze_images(images, instruction)` defined in Task 3 Step 2 and consumed by both `_capture_and_analyze` (Step 3) and `_capture_scrolling_and_analyze` (Step 4). `open_on_screen_impl` defined in Task 1 Step 3 and imported by the Task 1 tests. `_resolve_and_check` reused from `friday/tools/files.py` (returns a contained `Path` or `None`), with an added `.exists()` check for the auto/file branches.
