"""Bass hits for the wallpaper to pulse to (friday/music_pulse.py)."""
import json
import sys
import time
import urllib.request

import numpy as np
import pytest

from friday.music_pulse import RATE, BassTracker, MusicPulse

BPM = 120


def _kick(rng_len=0.15):
    t = np.arange(int(rng_len * RATE)) / RATE
    pitch = 50 + 90 * np.exp(-t * 30)                   # the usual kick: a fast pitch drop
    return np.sin(2 * np.pi * np.cumsum(pitch) / RATE) * np.exp(-t * 18)


def _song(seconds=8.0, kicks=True, seed=1):
    """Hi-hats and a sustained bass line under a four-on-the-floor kick."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    song = 0.05 * np.sin(2 * np.pi * 98 * t)            # a steady bass note: not a beat
    hats = rng.normal(0, 1, n) * 0.04
    hats = hats - np.convolve(hats, np.ones(8) / 8, mode="same")    # crude high-pass
    song += hats
    if kicks:
        kick = _kick()
        for start in np.arange(0.5, seconds - 0.2, 60 / BPM):
            i = int(start * RATE)
            song[i:i + len(kick)] += 0.6 * kick[: n - i]
    return np.clip(song, -1, 1).astype(np.float32)


def _feed(tracker, samples, chunk=512):
    for i in range(0, len(samples), chunk):
        tracker.feed(samples[i:i + chunk])


def test_finds_the_kicks():
    tracker = BassTracker()
    _feed(tracker, _song())
    expected = len(np.arange(0.5, 8.0 - 0.2, 60 / BPM))
    # The first second or so trains the threshold.
    assert expected - 3 <= tracker.beat <= expected


def test_no_kicks_no_beats():
    tracker = BassTracker()
    _feed(tracker, _song(kicks=False))
    assert tracker.beat <= 1


def test_silence_is_nothing():
    tracker = BassTracker()
    _feed(tracker, np.zeros(RATE * 3, dtype=np.float32))
    assert tracker.beat == 0 and tracker.level == 0.0


def test_level_follows_the_bass():
    tracker = BassTracker()
    song = _song(seconds=4)
    levels = []
    for i in range(0, len(song), 256):
        tracker.feed(song[i:i + 256])
        levels.append(tracker.level)
    assert max(levels[len(levels) // 2:]) > 0.8        # on the kicks
    assert min(levels[len(levels) // 2:]) < 0.3        # between them
    _feed(tracker, np.zeros(RATE, dtype=np.float32))
    assert tracker.level == 0.0


# A stand-in for parec: the same song, in real time, as s16le on stdout.
_FAKE_PAREC = f"""
import sys, time, numpy as np
sys.path.insert(0, {repr(sys.path[0])})
from tests.test_music_pulse import _song
pcm = (_song(seconds=30) * 32767).astype('<i2').tobytes()
step = 512 * 2
for i in range(0, len(pcm), step):
    sys.stdout.buffer.write(pcm[i:i + step]); sys.stdout.buffer.flush()
    time.sleep(512 / {RATE})
"""


@pytest.fixture
def pulse():
    p = MusicPulse(port=0, idle=1.0,
                   capture_cmd=lambda: [sys.executable, "-c", _FAKE_PAREC])
    p.start()
    assert p.listening(5)
    yield p
    p.stop()


def _get(p, after):
    with urllib.request.urlopen(f"http://127.0.0.1:{p.port}/music?after={after}", timeout=5) as r:
        return json.loads(r.read())


def test_beats_arrive_over_http_and_capture_stops_when_nobody_asks(pulse):
    assert not pulse.capturing
    reply = _get(pulse, -1)                 # asking starts capture
    beat = reply["beat"]
    deadline = time.monotonic() + 8
    while beat < 3 and time.monotonic() < deadline:
        beat = _get(pulse, beat)["beat"]
    assert beat >= 3
    assert pulse.capturing
    time.sleep(2.5)                         # nobody asking for longer than `idle`
    assert not pulse.capturing


def test_nothing_new_answers_with_the_level(pulse):
    started = time.monotonic()
    reply = _get(pulse, 10**6)
    assert set(reply) == {"beat", "level"}
    assert time.monotonic() - started < 2
