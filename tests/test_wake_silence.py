"""False wake words on a near-silent mic.

Logs 2026-08-01 → 2026-09-10: 47 of 69 "speaker mismatch" rejections scored
exactly sim=0.630/0.631. Reproduced offline: Resemblyzer scores digital silence
(and noise below ~1e-4) at exactly 0.631 against the enrolled embedding. So
openwakeword was firing on a silent mic — at 02:53, 13:53, etc. — and each false
wake flashed the overlay, sent PREPARE/ABORT to the agent and ran Resemblyzer.
Any buffer quiet enough to trip this check scores < 0.63, below the 0.70
threshold, so skipping it early never turns a real accept into a reject.
"""
import numpy as np  # real numpy first, so the launcher import helper never stubs it

from tests.test_session_recovery import _patch_and_import_launcher

RATE = 16000
N = int(RATE * 2.5)


def _launcher():
    return _patch_and_import_launcher()


def test_digital_silence_is_near_silent():
    assert _launcher()._is_near_silent(np.zeros(N, dtype=np.float32))


def test_mic_noise_floor_is_near_silent():
    rng = np.random.default_rng(0)
    floor = (rng.standard_normal(N) * 1e-4).astype(np.float32)
    assert _launcher()._is_near_silent(floor)


def test_spoken_wake_word_is_not_silent():
    """~0.8s of quiet speech inside an otherwise silent 2.5s buffer."""
    t = np.arange(int(RATE * 0.8)) / RATE
    buf = np.zeros(N, dtype=np.float32)
    buf[-len(t):] = 0.05 * np.sin(2 * np.pi * 220 * t)
    assert not _launcher()._is_near_silent(buf)
