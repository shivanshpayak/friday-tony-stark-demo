"""
Cut the silence the TTS pads around every line.

Gemini TTS wraps each utterance in silence: 160-210 ms before the first word
and 390-480 ms after the last (measured 2026-10-11 on the cached wake lines and
on live replies). Both are waits the user sits through:

  * before — the reply is audible that much later than it could be;
  * after  — the mic is closed while the agent "speaks" (see the interruption
    options in agent_friday.py), and after a wake it only opens once the ready
    line has finished, so the padding is dead air before the user can talk.

Only the two ends are cut. Pauses inside a reply pass through untouched.
"""

from typing import AsyncIterable, AsyncIterator

import numpy as np

# Peak below this is silence: about -40 dBFS. The padding itself never
# exceeded 241 (of 32767) in the measured clips.
SILENCE_PEAK = 330
# Kept ahead of the first audible sample so a soft onset isn't clipped.
KEEP_LEAD_MS = 20
# Kept after the last audible sample. Deliberately not shorter: the mic reopens
# when playback is reported done, and the sound card is still emptying its
# buffer then — the tail is what keeps the agent from hearing its own last word.
KEEP_TAIL_MS = 150


def _audible_span(samples: np.ndarray, num_channels: int) -> tuple[int, int] | None:
    """(first, last) audible sample-frame index, or None if all silence."""
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > SILENCE_PEAK)
    if loud.size == 0:
        return None
    return int(loud[0]) // num_channels, int(loud[-1]) // num_channels


def trim_pcm(pcm: bytes, sample_rate: int, num_channels: int) -> bytes:
    """Return 16-bit `pcm` with its leading and trailing silence cut."""
    step = 2 * num_channels
    span = _audible_span(np.frombuffer(pcm[:len(pcm) - len(pcm) % step], np.int16),
                         num_channels)
    if span is None:
        return pcm
    start = max(0, span[0] - sample_rate * KEEP_LEAD_MS // 1000)
    stop = span[1] + 1 + sample_rate * KEEP_TAIL_MS // 1000
    return pcm[start * step:stop * step]


async def trim_edge_silence(frames: AsyncIterable) -> AsyncIterator:
    """Yield a TTS frame stream without its leading and trailing silence.

    Silence is held back until audio follows it, so a pause between sentences
    is replayed in full and only the silence that ends the stream is dropped.
    Frames arrive much faster than they play, so holding them costs nothing.
    """
    from livekit import rtc

    def cut(frame, start: int, stop: int):
        step = 2 * frame.num_channels
        return rtc.AudioFrame(
            data=bytes(frame.data)[start * step:stop * step],
            sample_rate=frame.sample_rate, num_channels=frame.num_channels,
            samples_per_channel=stop - start)

    started = False
    lead = b""           # the end of the leading silence dropped so far
    tail_owed = 0        # silence still to play after the last audible sample
    held: list = []      # silence seen since then, replayed only if audio follows
    async for frame in frames:
        step = 2 * frame.num_channels
        total = frame.samples_per_channel
        span = _audible_span(np.frombuffer(frame.data, np.int16), frame.num_channels)
        if span is None:
            if not started:
                keep = frame.sample_rate * KEEP_LEAD_MS // 1000 * step
                lead = (lead + bytes(frame.data))[-keep:]
                continue
            now = min(tail_owed, total)
            tail_owed -= now
            if now:
                yield cut(frame, 0, now)
            if now < total:
                held.append(cut(frame, now, total))
            continue

        first, last = span
        begin = 0
        if not started:
            started = True
            want = frame.sample_rate * KEEP_LEAD_MS // 1000
            begin = max(0, first - want)
            missing = (want - first) * step
            if missing > 0 and lead:
                lead = lead[-missing:]
                yield rtc.AudioFrame(
                    data=lead, sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                    samples_per_channel=len(lead) // step)
        for silent in held:
            yield silent
        held.clear()
        tail = frame.sample_rate * KEEP_TAIL_MS // 1000
        end = min(total, last + 1 + tail)
        tail_owed = tail - (end - last - 1)
        yield cut(frame, begin, end)
        if end < total:
            held.append(cut(frame, end, total))
