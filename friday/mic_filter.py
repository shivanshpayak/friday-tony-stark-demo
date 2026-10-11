"""
Take mains hum out of the mic before speech-to-text hears it.

This laptop's analog mic picks up a pure 120 Hz tone at about -20 dBFS while
charging — as loud as speech. Both the local VAD and Deepgram's endpointing
took it for a voice that never stops, so a finished request never became a
turn and JARVIS sat silent. A steep high-pass at 250 Hz removes the hum (and
its 50/60/100/180 Hz relatives); speech recognition does not need what is
below it.

Streaming: the filter keeps its state between frames, so there is no click at
frame edges. Costs microseconds per 10 ms frame.
"""

import numpy as np
from livekit import rtc
from scipy.signal import butter, sosfilt

CUTOFF_HZ = 250
_ORDER = 6


class HumFilter:
    """High-pass for a stream of int16 audio frames at one sample rate."""

    def __init__(self) -> None:
        self._rate = 0
        self._sos = None
        self._state = None

    def samples(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        """Filter int16 mono samples; returns int16."""
        if rate != self._rate:
            self._rate = rate
            self._sos = butter(_ORDER, CUTOFF_HZ, "highpass", fs=rate, output="sos")
            self._state = np.zeros((self._sos.shape[0], 2))
        out, self._state = sosfilt(self._sos, pcm.astype(np.float64), zi=self._state)
        return np.clip(out, -32768, 32767).astype(np.int16)

    def frame(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if frame.num_channels != 1:
            return frame   # the session mic is mono; leave anything else alone
        pcm = np.frombuffer(frame.data, dtype=np.int16)
        return rtc.AudioFrame(
            data=self.samples(pcm, frame.sample_rate).tobytes(),
            sample_rate=frame.sample_rate, num_channels=1,
            samples_per_channel=frame.samples_per_channel)
