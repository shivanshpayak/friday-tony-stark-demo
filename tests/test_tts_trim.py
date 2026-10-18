"""The silence Gemini TTS pads around a line is cut before it plays.

Measured 2026-10-11: 160-210 ms before the first word and 390-480 ms after the
last, on every wake line and every reply. The mic is closed until playback
ends, so that padding was time the user waited before being able to speak.
"""
import asyncio

import numpy as np
from livekit import rtc

from friday import voice_cache as vc
from friday.tts_trim import KEEP_LEAD_MS, KEEP_TAIL_MS, trim_edge_silence, trim_pcm

RATE = 24000


def _ms(n: float) -> int:
    return int(RATE * n / 1000)


def _silence(ms: float) -> np.ndarray:
    return np.full(_ms(ms), 40, dtype=np.int16)      # a noise floor, not zeros


def _voice(ms: float) -> np.ndarray:
    t = np.arange(_ms(ms)) / RATE
    return (8000 * np.sin(2 * np.pi * 220 * t + 1)).astype(np.int16)


def _frames(pcm: np.ndarray, chunk_ms: int = 200) -> list[rtc.AudioFrame]:
    return vc.to_frames(pcm.tobytes(), RATE, 1, chunk_ms=chunk_ms)


def _run(frames) -> np.ndarray:
    async def source():
        for f in frames:
            yield f

    async def drain():
        return [f async for f in trim_edge_silence(source())]

    out = asyncio.run(drain())
    assert all(f.samples_per_channel * 2 == len(bytes(f.data)) for f in out)
    return np.concatenate([np.frombuffer(f.data, np.int16) for f in out])


def test_padding_is_cut_from_a_cached_line():
    line = np.concatenate([_silence(170), _voice(600), _silence(420)])
    out = np.frombuffer(trim_pcm(line.tobytes(), RATE, 1), np.int16)

    assert abs(len(out) - _ms(600 + KEEP_LEAD_MS + KEEP_TAIL_MS)) <= _ms(2)
    voice = _voice(600)
    start = _ms(KEEP_LEAD_MS)
    assert np.array_equal(out[start + 50:start + 50 + 1000], voice[50:1050])


def test_a_silent_or_empty_clip_is_left_alone():
    quiet = _silence(300).tobytes()
    assert trim_pcm(quiet, RATE, 1) == quiet
    assert trim_pcm(b"", RATE, 1) == b""


def test_stream_loses_only_its_two_ends():
    """The pause between two sentences must survive; only the ends go."""
    reply = np.concatenate([_silence(160), _voice(700), _silence(350),
                            _voice(500), _silence(400)])
    out = _run(_frames(reply))

    kept = 700 + 350 + 500 + KEEP_LEAD_MS + KEEP_TAIL_MS
    assert abs(len(out) - _ms(kept)) <= _ms(2)
    # Everything from the first word to the last is byte-for-byte unchanged.
    body = reply[_ms(160):_ms(160 + 700 + 350 + 500)]
    assert np.array_equal(out[_ms(KEEP_LEAD_MS):_ms(KEEP_LEAD_MS) + len(body)], body)


def test_stream_result_does_not_depend_on_frame_size():
    reply = np.concatenate([_silence(160), _voice(700), _silence(400)])
    assert np.array_equal(_run(_frames(reply, 200)), _run(_frames(reply, 20)))


def test_stream_of_pure_silence_plays_nothing():
    async def source():
        for f in _frames(_silence(500)):
            yield f

    async def drain():
        return [f async for f in trim_edge_silence(source())]

    assert asyncio.run(drain()) == []


def test_cached_wake_lines_are_replayed_trimmed(tmp_path):
    """The file on disk stays as rendered; what plays has the padding cut."""
    text = "Ready when you are, sir."
    raw = np.concatenate([_silence(170), _voice(600), _silence(420)]).tobytes()
    vc.save_pcm(vc.cache_path(text, "Charon", tmp_path), raw, RATE, 1)

    lines = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    assert asyncio.run(lines.ensure(None, [text])) == 1

    played = sum(f.samples_per_channel for f in lines.get(text))
    assert abs(played - _ms(600 + KEEP_LEAD_MS + KEEP_TAIL_MS)) <= _ms(2)
    assert vc.load_pcm(vc.cache_path(text, "Charon", tmp_path))[0] == raw


# --- long replies -----------------------------------------------------------

def test_a_long_reply_is_given_time_to_finish():
    """livekit's 10s TTS timeout is the Google plugin's deadline for the whole
    stream, so any reply that took longer to synthesise was cut off mid-word.
    Retries are off because they re-read text that is already consumed."""
    from friday.providers import build_session_conn_options

    tts = build_session_conn_options().tts_conn_options
    assert tts.timeout >= 120
    assert tts.max_retry == 0


def test_a_tts_that_never_starts_is_still_caught():
    import agent_friday
    from livekit.agents import APITimeoutError

    async def never():
        await asyncio.sleep(5)
        yield "frame"

    async def drain(source, timeout):
        return [f async for f in agent_friday._first_within(source, timeout)]

    try:
        asyncio.run(drain(never(), 0.05))
        raise AssertionError("expected a timeout")
    except APITimeoutError:
        pass


def test_slow_audio_after_the_first_frame_is_not_cut():
    import agent_friday

    async def slow():
        yield 1
        await asyncio.sleep(0.2)        # longer than the first-frame limit
        yield 2

    async def empty():
        return
        yield

    async def drain(source):
        return [f async for f in agent_friday._first_within(source, 0.05)]

    assert asyncio.run(drain(slow())) == [1, 2]
    assert asyncio.run(drain(empty())) == []
