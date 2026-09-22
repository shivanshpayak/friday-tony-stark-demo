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
