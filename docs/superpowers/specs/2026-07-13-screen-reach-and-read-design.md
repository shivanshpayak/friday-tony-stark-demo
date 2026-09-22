# Screen Reach & Read — Design

**Date:** 2026-07-13
**Status:** Approved, ready for implementation plan

## Problem

Two gaps in FRIDAY's relationship with the screen:

1. **Reach.** FRIDAY can *speak* web-search summaries but has no way to "pull up"
   arbitrary content — a URL, a Google search, or a local file/folder — in front
   of the user. The building block exists (`open_world_monitor` and
   `create_document` in [friday/tools/web.py](../../../friday/tools/web.py) call
   `webbrowser.open` / `os.startfile`), but only for a few hardcoded destinations.
2. **Read past the viewport.** `read_screen`
   ([friday/tools/screen.py](../../../friday/tools/screen.py)) captures only what's
   *visible*. Long content — a Spotify playlist, a chat log, a long article —
   scrolls off screen. Modern apps use **virtualized lists**: off-screen rows
   don't exist in the app's memory/DOM/accessibility tree until scrolled into
   view. So there is no one-shot API/accessibility read that captures the whole
   thing — the only way to see all of it is to **scroll through it and capture as
   you go**.

These are two independent features, specced together because they were requested
together and both concern FRIDAY's reach to/from the screen. They can ship
separately.

## Feature A — `open_on_screen` ("pull up anything")

### Behavior

One new MCP tool, added to `friday/tools/web.py`:

```
open_on_screen(target: str, kind: str = "auto") -> str
```

- `kind="url"` → `webbrowser.open(target)`.
- `kind="search"` → open `https://www.google.com/search?q=<urllib.parse.quote_plus(target)>`.
- `kind="file"` → resolve `target` via files.py's existing `_resolve_and_check`
  (handles aliases + `FRIDAY_FILE_ROOTS` containment). If it returns a path,
  `os.startfile(path)` (opens a file in its default app, a folder in Explorer).
  If it returns `None`, refuse: "That's outside the folders I can open, sir."
- `kind="auto"` (default) — decide by inspecting `target`:
  1. Looks like a URL/bare domain (`urllib.parse.urlparse` has a scheme, or it
     matches a `domain.tld[/...]` pattern) → treat as **url** (prepend `https://`
     if no scheme).
  2. Else if `_resolve_and_check(target)` returns a real existing path → **file**.
  3. Else → **search**.

Returns a short spoken confirmation ("Pulling that up, sir."). The model gets
explicit control via `kind`, with a sensible default. ACTING OUT LOUD means
FRIDAY pre-announces before calling it.

### Safety

- File opens are confined to `FRIDAY_FILE_ROOTS` via `_resolve_and_check` — no
  path outside the approved roots can be opened.
- URL/search opens are non-destructive (they only launch the browser).

## Feature B — `read_long_content` (see past the viewport)

Two units — a pure capture engine and the tool — mirroring the loop engine's
split (pure, testable core + thin tool wrapper).

### `friday/screen_scroll.py` — pure capture engine

```python
def average_hash(image) -> int:
    """64-bit average hash: resize to 8x8 grayscale, threshold at the mean bit."""

def hamming(a: int, b: int) -> int:
    """Bit differences between two average hashes."""

def capture_scrolling(
    grab_fn: Callable[[], Any],          # returns a frame (PIL image in prod)
    scroll_fn: Callable[[], None],       # scrolls the target down one viewport
    *,
    max_scrolls: int = 12,
    settle_sleep: Callable[[float], None] = time.sleep,
    settle_seconds: float = 0.4,
    hash_fn: Callable[[Any], int] = average_hash,
    stable_distance: int = 2,            # Hamming <= this => "no change" => bottom
) -> list:
    """Grab -> scroll -> grab, collecting frames until two consecutive frames are
    ~identical (reached the bottom) or max_scrolls is hit. Returns the frames."""
```

Loop: grab first frame; then up to `max_scrolls` times: `scroll_fn()`,
`settle_sleep(settle_seconds)`, grab; if `hamming(hash_fn(prev), hash_fn(cur)) <=
stable_distance` → stop (bottom reached); else append and continue. `grab_fn`,
`scroll_fn`, and `settle_sleep` are injected so the loop is unit-testable with
fakes — no real screen, no real time, no `pyautogui`. Average-hash is hand-rolled
(PIL only) so no new hashing dependency.

### `friday/tools/screen.py` — the tool + multi-image vision

- **Refactor:** extract the existing single-image Gemini call into
  `_analyze_images(images: list, instruction: str) -> str` (builds one request
  with N `inline_data` parts + the instruction text, keeps the current retry
  loop). `_capture_and_analyze` (the existing `read_screen`) becomes a
  single-element call through it — behavior-preserving.
- **New tool:**
  ```
  read_long_content(prompt: str = "") -> str
  ```
  1. Guard-import `pyautogui`; if missing, return a friendly message.
  2. Build the real `scroll_fn`: move the mouse to the active window's center
     (Win32 `GetForegroundWindow`+`GetWindowRect` via `ctypes`, falling back to
     screen center) and `pyautogui.scroll(-SCROLL_CLICKS)` where `SCROLL_CLICKS`
     is a module constant defaulting to `10` (roughly one viewport of wheel
     notches; tuned during live verification). Negative = scroll down.
  3. `frames = capture_scrolling(grab_fn=ImageGrab.grab, scroll_fn=scroll_fn)`.
  4. Downscale frames (reuse the `_MAX_DIM` thumbnail step) and call
     `_analyze_images(frames, instruction)` where the instruction says: "These are
     sequential top-to-bottom screenshots of a scrolling view. Read the content in
     order and ignore repeated/overlapping rows between consecutive images. The
     user asked: {prompt}. Answer concisely for spoken delivery."
  5. Return the vision answer.

### Behavior & caveats

- **Slow/Task-class (~5–9s).** FRIDAY speaks a pre-line ("Reading through it now,
  sir.") then does the capture+vision inline, then answers — the pre-line fills
  the dead air. Inline (not a background task) because the answer *is* the reply.
- **It visibly scrolls the active window.** Inherent to the approach.
- **Ingests everything, speaks a summary.** It sees all captured content but
  answers the user's `prompt` (count, presence of an item, highlights) rather than
  reading a 200-row list aloud. Follow-up questions work against the same view if
  asked again (each call re-captures; no caching in v1).
- **Frame cap:** `max_scrolls=12` → ≤13 images per vision call, bounding cost and
  latency.

### Safety & error handling

- Scrolling is non-destructive input (no clicks/activation), bounded by
  `max_scrolls`; the launcher kill switch still halts everything.
- `pyautogui` import guarded → friendly "I need the pyautogui component" message
  if unavailable.
- Active-window lookup failure → fall back to screen center.
- Capture yields 0–1 frames (short page, nothing to scroll) → still answers from
  what was captured (degrades to `read_screen` behavior).
- Vision call failure → the existing retry loop's error string.

## Dependencies

- Add `pyautogui` to `pyproject.toml`. Used only by Feature B's real `scroll_fn`
  (and mouse positioning). It is also the intended foundation for the future
  desktop-automation roadmap item.

## Testing

- **Unit (pytest, like `tests/test_looping_*.py`):**
  - `capture_scrolling` with a fake `grab_fn` returning a sequence that stabilizes
    after *k* frames (fake `scroll_fn` counter, fake `settle_sleep`): stops at the
    stable point; returns the expected frame count.
  - `capture_scrolling` where frames never stabilize: stops at `max_scrolls`
    (returns `max_scrolls`+1 frames).
  - `average_hash`/`hamming`: identical images → distance 0; clearly different
    images → large distance; `stable_distance` boundary respected.
  - Feature A: `open_on_screen` kind-detection is exercised with `webbrowser.open`
    / `os.startfile` monkeypatched (assert URL vs search vs file routing and that
    an out-of-roots path is refused) — no real browser/file launched.
- **Live verification:**
  - A: "pull up the weather in Tokyo" → Google results open; "open my downloads
    folder" → Explorer opens; "open github.com" → site opens.
  - B: open a long Spotify playlist → "how many songs is this?" / "is <track> in
    it?" answered from beyond the visible rows; a short window → quick answer, few
    frames.

## Documentation

Update `ARCHITECTURE.md` §7 (Feature map) / §8 and the "Where things live" index
to add `open_on_screen` and `read_long_content` + `friday/screen_scroll.py`.

## Scope Summary

- **New:** `friday/screen_scroll.py`; `read_long_content` + `_analyze_images`
  refactor in `friday/tools/screen.py`; `open_on_screen` in `friday/tools/web.py`;
  `pyautogui` dependency; tests; `ARCHITECTURE.md` update.
- **Unchanged:** existing `read_screen` behavior (refactor is behavior-preserving),
  the live reply path, and every other tool.
- **Independent features:** A and B share nothing but the theme; either can be
  built/shipped without the other.
- **Git:** left uncommitted for the owner per their standing preference.
