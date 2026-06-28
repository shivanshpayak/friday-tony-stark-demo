"""Fast, CPU-cheap VAD using Google's WebRTC VAD via the `webrtcvad` C package.

Why this exists: Silero VAD via the livekit-plugins-silero ONNX path runs at
~440 ms/frame on this machine — far slower than realtime (10 ms/frame). The
audio pipeline backs up minutes deep and the agent appears frozen.

WebRTC VAD is ~100x faster (microseconds per frame). The catch is it's noisier
(triggers on keystrokes, fan noise). That's acceptable here because the
enrolled speaker gate filters non-user transcripts downstream.

Supported sample rates: 8000, 16000, 32000, 48000 Hz.
Supported frame sizes: 10, 20, or 30 ms (so at 16 kHz: 160, 320, 480 samples).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import numpy as np
import webrtcvad
from livekit import rtc
from livekit.agents import vad as lk_vad
from livekit.agents.utils import aio


# WebRTC VAD operates on 16 kHz mono int16 in 20 ms windows by convention.
_VAD_SAMPLE_RATE = 16000
_VAD_FRAME_MS = 20
_VAD_SAMPLES_PER_FRAME = _VAD_SAMPLE_RATE * _VAD_FRAME_MS // 1000  # 320


class WebRTCVAD(lk_vad.VAD):
    """Fast VAD using google/webrtcvad. Drop-in replacement for silero.VAD.

    Args:
        aggressiveness: 0 (most permissive) to 3 (most restrictive). 2 is a
            sensible default — catches typical speech without being trigger-
            happy on keyboard noise.
        min_speech_frames: Number of consecutive speech windows needed to
            declare START_OF_SPEECH. At 20 ms/window, 5 = 100 ms minimum
            speech duration.
        min_silence_frames: Number of consecutive silent windows needed to
            declare END_OF_SPEECH. At 20 ms/window, 25 = 500 ms silence.
    """

    def __init__(
        self,
        *,
        aggressiveness: int = 2,
        min_speech_frames: int = 5,
        min_silence_frames: int = 25,
    ) -> None:
        super().__init__(
            capabilities=lk_vad.VADCapabilities(update_interval=_VAD_FRAME_MS / 1000.0),
        )
        self._aggressiveness = aggressiveness
        self._min_speech_frames = min_speech_frames
        self._min_silence_frames = min_silence_frames

    @property
    def model(self) -> str:
        return f"webrtcvad-{self._aggressiveness}"

    @property
    def provider(self) -> str:
        return "google"

    def stream(self) -> lk_vad.VADStream:
        return _WebRTCVADStream(
            self,
            aggressiveness=self._aggressiveness,
            min_speech_frames=self._min_speech_frames,
            min_silence_frames=self._min_silence_frames,
        )


def _resample_to_16k(samples: np.ndarray, in_rate: int) -> np.ndarray:
    """Linear-interpolation resample to 16 kHz. Simple but fast.

    Quality is fine for VAD (which only needs broad energy/spectral shape).
    Returns int16 mono samples at 16 kHz.
    """
    if in_rate == _VAD_SAMPLE_RATE:
        return samples
    if samples.ndim != 1:
        samples = samples.reshape(-1)
    n_in = samples.size
    n_out = int(round(n_in * _VAD_SAMPLE_RATE / in_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    x_in = np.arange(n_in, dtype=np.float32)
    x_out = np.linspace(0, n_in - 1, n_out, dtype=np.float32)
    y = np.interp(x_out, x_in, samples.astype(np.float32))
    return y.astype(np.int16)


class _WebRTCVADStream(lk_vad.VADStream):
    def __init__(
        self,
        vad: WebRTCVAD,
        *,
        aggressiveness: int,
        min_speech_frames: int,
        min_silence_frames: int,
    ) -> None:
        self._wrtc = webrtcvad.Vad(aggressiveness)
        self._min_speech_frames = min_speech_frames
        self._min_silence_frames = min_silence_frames
        # Persistent buffer of int16 samples at 16 kHz waiting to be sliced
        # into 20 ms windows.
        self._pcm_buf = np.zeros(0, dtype=np.int16)
        self._speaking = False
        self._consec_speech = 0
        self._consec_silence = 0
        self._sample_index = 0  # for VADEvent.samples_index
        # Ring buffer of recent input audio frames (for the prefix-padding
        # included in START_OF_SPEECH events). ~500 ms at typical frame rates.
        self._recent_frames: deque[rtc.AudioFrame] = deque(maxlen=50)
        self._speech_frames: list[rtc.AudioFrame] = []
        self._speech_start_time: float = 0.0
        self._silence_start_time: float = 0.0
        super().__init__(vad)

    async def _main_task(self) -> None:
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                continue
            self._process_frame(item)

    def _process_frame(self, frame: rtc.AudioFrame) -> None:
        # Convert frame bytes to int16 samples, resample to 16 kHz, append
        # to the rolling buffer, slice into 20 ms windows and pass each
        # through webrtcvad.
        raw = np.frombuffer(frame.data, dtype=np.int16)
        if frame.num_channels > 1:
            raw = raw.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        resampled = _resample_to_16k(raw, frame.sample_rate)
        self._pcm_buf = np.concatenate([self._pcm_buf, resampled])
        self._recent_frames.append(frame)

        while self._pcm_buf.size >= _VAD_SAMPLES_PER_FRAME:
            window = self._pcm_buf[:_VAD_SAMPLES_PER_FRAME]
            self._pcm_buf = self._pcm_buf[_VAD_SAMPLES_PER_FRAME:]
            self._handle_window(window, frame)

    def _handle_window(self, window: np.ndarray, source_frame: rtc.AudioFrame) -> None:
        inference_start = time.perf_counter()
        is_speech = self._wrtc.is_speech(window.tobytes(), _VAD_SAMPLE_RATE)
        inference_duration = time.perf_counter() - inference_start
        self._sample_index += _VAD_SAMPLES_PER_FRAME

        if is_speech:
            self._consec_speech += 1
            self._consec_silence = 0
        else:
            self._consec_silence += 1
            self._consec_speech = 0

        now = time.time()

        # Always emit INFERENCE_DONE so livekit's metrics + endpointing track
        # what's happening per window.
        self._event_ch.send_nowait(lk_vad.VADEvent(
            type=lk_vad.VADEventType.INFERENCE_DONE,
            samples_index=self._sample_index,
            timestamp=now,
            speech_duration=now - self._speech_start_time if self._speaking else 0.0,
            silence_duration=now - self._silence_start_time if not self._speaking else 0.0,
            probability=1.0 if is_speech else 0.0,
            inference_duration=inference_duration,
            speaking=self._speaking,
        ))

        # State transition: silent → speaking
        if not self._speaking and self._consec_speech >= self._min_speech_frames:
            self._speaking = True
            self._speech_start_time = now
            # Include the recent frames as prefix padding so STT gets the
            # leading edge of the utterance.
            prefix = list(self._recent_frames)
            self._speech_frames = list(prefix)
            self._event_ch.send_nowait(lk_vad.VADEvent(
                type=lk_vad.VADEventType.START_OF_SPEECH,
                samples_index=self._sample_index,
                timestamp=now,
                speech_duration=0.0,
                silence_duration=0.0,
                frames=prefix,
                probability=1.0,
                speaking=True,
            ))
            return

        # State transition: speaking → silent
        if self._speaking:
            self._speech_frames.append(source_frame)
            if self._consec_silence >= self._min_silence_frames:
                self._speaking = False
                self._silence_start_time = now
                frames = self._speech_frames
                self._speech_frames = []
                self._event_ch.send_nowait(lk_vad.VADEvent(
                    type=lk_vad.VADEventType.END_OF_SPEECH,
                    samples_index=self._sample_index,
                    timestamp=now,
                    speech_duration=now - self._speech_start_time,
                    silence_duration=0.0,
                    frames=frames,
                    probability=0.0,
                    speaking=False,
                ))
