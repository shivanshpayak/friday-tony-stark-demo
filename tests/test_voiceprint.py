"""Clips are cleaned (silence cut, volume levelled) before fingerprinting.

The 2.5s behind a wake word is mostly room noise; raw, that noise moved the
owner's score under the bar whenever the room changed.
"""
import wave
from pathlib import Path

import numpy as np
import pytest

from friday.voiceprint import clean_clip, highpass

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


def _tone(hz: float, seconds: float = 1.0, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(16000 * seconds)) / 16000
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)))


def test_highpass_drops_rumble_and_hum_and_keeps_speech_band():
    for hz in (20, 54, 120, 240):
        assert _rms(highpass(_tone(hz))) < 0.01 * _rms(_tone(hz)), hz
    for hz in (400, 1000, 3000):
        assert abs(_rms(highpass(_tone(hz))) / _rms(_tone(hz)) - 1) < 0.02, hz


def test_mic_rumble_and_hum_do_not_move_the_fingerprint():
    """The same words, with and without the low-frequency noise this mic adds,
    must embed to nearly the same vector — unfiltered they came out as far
    apart as two different people (the owner passed ~1 wake in 10)."""
    from friday.voice_encoder import VoiceEncoder

    speech = np.zeros(40000, dtype=np.float32)
    speech[20000:36000] = 0.5 * _speech_second()
    rumble = 2.0 * _tone(20, 2.5, 0.03) + _tone(54, 2.5, 0.02)
    hum = _tone(120, 2.5, 0.1)

    encoder = VoiceEncoder()
    clean = encoder.embed_utterance(clean_clip(speech))
    for noise in (rumble, hum, rumble + hum):
        noisy = encoder.embed_utterance(clean_clip(speech + noise))
        assert float(clean @ noisy) > 0.95
