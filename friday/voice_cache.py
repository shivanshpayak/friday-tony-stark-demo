"""Pre-rendered voice lines — replay fixed phrases without an LLM or TTS call.

Every wake used to run `session.generate_reply(_READY_LINE_INSTRUCTIONS)`: a
full Gemini round-trip to pick one of three phrases the prompt already listed,
then a Google TTS synthesis of it. The mic is only enabled once that line
finishes playing, so the whole thing sat on the wake→listening path — measured
at a 3.76s median (mean 4.16s, max 5.30s) across the activations in
logs/friday.log on 2026-09-22.

Those phrases never change, so they are synthesised once, cached as WAV under
runtime/voice_cache/ (keyed by voice + text, so retuning the voice invalidates
them), and replayed via `session.say(text, audio=...)` — which forwards the
frames straight to the audio output, skipping both round-trips.

The mic opens when the line ends, so the silence Gemini pads around it is cut
before replay (friday/tts_trim.py) — about 0.4s off every wake.

Everything here degrades to None/0 on any failure so the caller can fall back
to the live path. Guarded by tests/test_voice_cache.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import random
import wave
from pathlib import Path
from typing import AsyncIterator, Sequence

from .config import logger
from .tts_trim import trim_pcm

# Repo-root/runtime/voice_cache. FRIDAY_VOICE_CACHE_DIR lets tests (and the MCP
# server, which has no business writing here) point somewhere else.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "runtime" / "voice_cache"

# 20 ms of audio per frame — what livekit's console output expects to pace.
DEFAULT_CHUNK_MS = 20
_SAMPLE_WIDTH = 2  # 16-bit PCM


def _cache_dir(cache_dir: Path | str | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    return Path(os.getenv("FRIDAY_VOICE_CACHE_DIR") or DEFAULT_CACHE_DIR)


def cache_key(text: str, voice: str) -> str:
    """Stable id for a (voice, line) pair. Voice is part of the key so that
    changing TTS voice re-renders instead of replaying the old one."""
    return hashlib.sha1(f"{voice}\x00{text}".encode("utf-8")).hexdigest()[:16]


def cache_path(text: str, voice: str, cache_dir: Path | str | None = None) -> Path:
    return _cache_dir(cache_dir) / f"{cache_key(text, voice)}.wav"


# ---------------------------------------------------------------------------
# WAV persistence
# ---------------------------------------------------------------------------

def save_pcm(path: Path, pcm: bytes, sample_rate: int, num_channels: int) -> None:
    """Write PCM to `path` atomically — a half-written cache file would be
    replayed as a truncated greeting on every future boot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".wav.tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(_SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    os.replace(tmp, path)


def load_pcm(path: Path) -> tuple[bytes, int, int] | None:
    """Read a cached line back. None if it's missing or unreadable."""
    try:
        with wave.open(str(path), "rb") as w:
            return (
                w.readframes(w.getnframes()),
                w.getframerate(),
                w.getnchannels(),
            )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

async def _collect(stream) -> tuple[bytes, int, int]:
    """Drain a TTS stream into raw PCM, always closing it."""
    pcm = bytearray()
    sample_rate = 0
    num_channels = 1
    try:
        async for ev in stream:
            frame = ev.frame
            sample_rate = frame.sample_rate
            num_channels = frame.num_channels
            pcm += bytes(frame.data)
    finally:
        # Never leak the provider's websocket/grpc stream, even on failure.
        with contextlib.suppress(Exception):
            await stream.aclose()
    return bytes(pcm), sample_rate, num_channels


async def render_pcm(tts, text: str) -> tuple[bytes, int, int]:
    """Synthesise `text` once and return its raw PCM, rate and channel count.

    Uses whichever API the provider actually supports. This matters: Gemini TTS
    (gemini-2.5-flash-tts) is streaming-only and answers the unary
    synthesize_speech path with `400 Unsupported audio encoding`, so rendering
    has to go through stream() — the same path the live session speaks on.
    """
    if getattr(getattr(tts, "capabilities", None), "streaming", False):
        stream = tts.stream()
        # Hand over the whole line and close the input: a streaming TTS keeps
        # waiting for more text otherwise, and never finishes the segment.
        stream.push_text(text)
        stream.flush()
        stream.end_input()
        return await _collect(stream)

    return await _collect(tts.synthesize(text))


def to_frames(pcm: bytes, sample_rate: int, num_channels: int,
              chunk_ms: int = DEFAULT_CHUNK_MS) -> list:
    """Slice PCM into AudioFrames. `rtc` is imported lazily so the cache logic
    stays testable (and importable) without dragging in livekit."""
    from livekit import rtc

    bytes_per_sample = _SAMPLE_WIDTH * num_channels
    step = max(1, (sample_rate * chunk_ms) // 1000) * bytes_per_sample

    frames = []
    for off in range(0, len(pcm), step):
        chunk = pcm[off:off + step]
        frames.append(rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=len(chunk) // bytes_per_sample,
        ))
    return frames


async def audio_stream(frames: Sequence) -> AsyncIterator:
    """Fresh async iterator over cached frames — say() consumes one per call."""
    for frame in frames:
        yield frame


# ---------------------------------------------------------------------------
# Line cache
# ---------------------------------------------------------------------------

class PrerenderedLines:
    """A voice's fixed lines, rendered once and held as ready-to-play frames."""

    def __init__(self, voice: str, cache_dir: Path | str | None = None) -> None:
        self.voice = voice
        self.cache_dir = _cache_dir(cache_dir)
        self._frames: dict[str, list] = {}

    async def ensure(self, tts, texts: Sequence[str]) -> int:
        """Make every line playable, rendering only what isn't cached yet.

        Returns how many are ready. Failures are logged and skipped rather than
        raised — a missing line just means the caller uses the live path.
        """
        for text in texts:
            if text in self._frames:
                continue
            path = cache_path(text, self.voice, self.cache_dir)

            cached = load_pcm(path)
            if cached is None:
                try:
                    pcm, sample_rate, num_channels = await render_pcm(tts, text)
                except Exception as e:
                    logger.warning("Voice cache: render failed for %r: %s", text, e)
                    continue
                if not pcm or not sample_rate:
                    logger.warning("Voice cache: empty audio for %r", text)
                    continue
                try:
                    save_pcm(path, pcm, sample_rate, num_channels)
                except Exception as e:
                    # Still playable this run, just not cached for the next.
                    logger.warning("Voice cache: could not write %s: %s", path, e)
                cached = (pcm, sample_rate, num_channels)

            try:
                # Trimmed here rather than on disk, so the cache keeps what the
                # TTS produced and a change to the trim needs no re-render.
                pcm, sample_rate, num_channels = cached
                self._frames[text] = to_frames(
                    trim_pcm(pcm, sample_rate, num_channels), sample_rate, num_channels)
            except Exception as e:
                logger.warning("Voice cache: could not frame %r: %s", text, e)

        return len(self._frames)

    def get(self, text: str) -> list | None:
        """Frames for `text`, or None if it never rendered."""
        return self._frames.get(text)

    def pick(self, rng: random.Random | None = None) -> tuple[str, list] | None:
        """A random rendered line, or None if nothing is available."""
        if not self._frames:
            return None
        chooser = rng or random
        text = chooser.choice(list(self._frames))
        return text, self._frames[text]
