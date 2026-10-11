"""Clips are cleaned (silence cut, volume levelled) before fingerprinting.

The 2.5s behind a wake word is mostly room noise; raw, that noise moved the
owner's score under the bar whenever the room changed.
"""
import wave
from pathlib import Path

import numpy as np
import pytest

from friday.voiceprint import clean_clip

SPEECH = Path(__file__).parents[1] / "sounds" / "booting_ack.wav"


def _speech_second() -> np.ndarray:
    from scipy.signal import resample_poly
    with wave.open(str(SPEECH)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return resample_poly(pcm.astype(np.float32) / 32768, 320, 441)[:16000].astype(np.float32)


def test_silence_before_the_word_is_cut():
    rng = np.random.default_rng(0)
    clip = np.concatenate([0.002 * rng.standard_normal(24000).astype(np.float32),
                           _speech_second()])
    assert len(clean_clip(clip)) < 0.6 * len(clip)


def test_clip_with_no_speech_falls_back_to_raw_length():
    rng = np.random.default_rng(0)
    noise = 0.002 * rng.standard_normal(40000).astype(np.float32)
    assert len(clean_clip(noise)) == len(noise)


@pytest.mark.parametrize("source", ["friday_launcher.py", "enroll_voice.py",
                                    "friday/speaker_gate.py"])
def test_enrollment_and_both_gates_clean_the_same_way(source):
    text = (Path(__file__).parents[1] / source).read_text(encoding="utf-8")
    assert "embed_utterance(clean_clip(" in text
    assert "embed_utterance(wav" not in text and "embed_utterance(clip)" not in text
