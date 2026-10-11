"""Mains hum is filtered out of the mic before speech-to-text.

A 120 Hz tone as loud as speech kept the STT's end-of-speech from ever firing.
"""
import numpy as np
from livekit import rtc

from friday.mic_filter import HumFilter


def _db(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean((x / 32768.0) ** 2)) + 1e-9)


def _tone(hz: float, rate: int, seconds: float = 1.0, amp: float = 4000) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def test_hum_is_removed_and_speech_band_kept():
    rate = 24000
    hum, voice = _tone(120, rate), _tone(1000, rate)
    assert _db(HumFilter().samples(hum, rate)[rate // 4:]) < _db(hum) - 30
    assert abs(_db(HumFilter().samples(voice, rate)[rate // 4:]) - _db(voice)) < 1


def test_frames_filter_as_one_continuous_stream():
    rate = 24000
    hum = _tone(120, rate)
    whole = HumFilter().samples(hum, rate)
    f, chunks = HumFilter(), []
    for i in range(0, len(hum), 240):                  # 10 ms frames
        frame = rtc.AudioFrame(data=hum[i:i + 240].tobytes(), sample_rate=rate,
                               num_channels=1, samples_per_channel=240)
        out = f.frame(frame)
        assert out.samples_per_channel == 240 and out.sample_rate == rate
        chunks.append(np.frombuffer(out.data, dtype=np.int16))
    assert np.array_equal(np.concatenate(chunks), whole)
