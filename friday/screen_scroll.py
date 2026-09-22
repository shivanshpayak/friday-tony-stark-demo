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
    pixels = list(small.tobytes())  # mode "L" => one byte per pixel, row-major
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
