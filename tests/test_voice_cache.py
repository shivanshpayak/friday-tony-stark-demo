"""Pre-rendered wake lines.

Waking FRIDAY cost a full Gemini call *plus* a Google TTS synthesis just to say
"Ready when you are, sir." — and the mic only opens once that line finishes
playing. Measured from logs/friday.log (2026-09-22, n=5): wake word → listening
ran 3.43s / 3.58s / 3.76s / 4.75s / 5.30s (median 3.76s).

The lines are fixed, so they are synthesised once, cached as WAV on disk, and
replayed through session.say(text, audio=...), which skips both round-trips.
"""
import asyncio
import types

import pytest

from friday import voice_cache as vc


# --- fakes -----------------------------------------------------------------

class _FakeStream:
    """Mimics TTS.synthesize(): async-iterable of SynthesizedAudio, + aclose()."""

    def __init__(self, chunks, sample_rate=24000, num_channels=1, fail=False):
        self._chunks = chunks
        self._sr = sample_rate
        self._ch = num_channels
        self._fail = fail
        self.closed = False

    def __aiter__(self):
        async def _gen():
            if self._fail:
                raise RuntimeError("tts exploded")
            for c in self._chunks:
                yield types.SimpleNamespace(
                    frame=types.SimpleNamespace(
                        data=c, sample_rate=self._sr, num_channels=self._ch
                    )
                )
        return _gen()

    async def aclose(self):
        self.closed = True


class _FakeTTS:
    """A TTS with no streaming support — render falls back to synthesize()."""

    capabilities = types.SimpleNamespace(streaming=False)

    def __init__(self, chunks=(b"\x01\x02" * 100,), fail=False):
        self._chunks = list(chunks)
        self._fail = fail
        self.calls = []
        self.streams = []

    def synthesize(self, text):
        self.calls.append(text)
        s = _FakeStream(self._chunks, fail=self._fail)
        self.streams.append(s)
        return s


class _FakeSynthesizeStream(_FakeStream):
    """Mimics TTS.stream(): text is pushed in, audio comes out."""

    def __init__(self, chunks, **kw):
        super().__init__(chunks, **kw)
        self.pushed = []
        self.flushed = False
        self.input_ended = False

    def push_text(self, token):
        assert not self.input_ended, "push_text after end_input"
        self.pushed.append(token)

    def flush(self):
        self.flushed = True

    def end_input(self):
        self.input_ended = True


class _FakeStreamingTTS:
    """What Google Gemini TTS actually is: streaming-only."""

    capabilities = types.SimpleNamespace(streaming=True)

    def __init__(self, chunks=(b"\x01\x02" * 100,), fail=False):
        self._chunks = list(chunks)
        self._fail = fail
        self.streams = []

    def stream(self):
        s = _FakeSynthesizeStream(self._chunks, fail=self._fail)
        self.streams.append(s)
        return s

    def synthesize(self, text):  # pragma: no cover - must never run
        raise AssertionError(
            "used the unary synthesize() path; Gemini TTS answers it with "
            "400 Unsupported audio encoding"
        )


class _ExplodingTTS:
    """Any use at all is a failure — proves a disk cache hit never calls TTS."""

    def synthesize(self, text):  # pragma: no cover - must never run
        raise AssertionError("TTS called despite a warm cache")


# --- key / path ------------------------------------------------------------

def test_cache_key_depends_on_text_and_voice():
    assert vc.cache_key("hello", "Charon") != vc.cache_key("hello", "Puck")
    assert vc.cache_key("hello", "Charon") != vc.cache_key("goodbye", "Charon")
    assert vc.cache_key("hello", "Charon") == vc.cache_key("hello", "Charon")


def test_cache_path_is_a_plain_filename(tmp_path):
    p = vc.cache_path("Ready when you are, sir.", "Charon", cache_dir=tmp_path)
    assert p.parent == tmp_path
    assert p.suffix == ".wav"
    # No punctuation/spaces from the line may leak into the filename.
    assert p.stem.isalnum()


# --- wav round-trip --------------------------------------------------------

def test_pcm_round_trips_through_wav(tmp_path):
    pcm = bytes(range(256)) * 4
    path = tmp_path / "line.wav"
    vc.save_pcm(path, pcm, sample_rate=24000, num_channels=1)

    assert vc.load_pcm(path) == (pcm, 24000, 1)


def test_load_pcm_returns_none_when_missing(tmp_path):
    assert vc.load_pcm(tmp_path / "nope.wav") is None


def test_load_pcm_returns_none_on_a_corrupt_file(tmp_path):
    """A half-written cache file must degrade to a re-render, not crash boot."""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    assert vc.load_pcm(bad) is None


# --- rendering -------------------------------------------------------------

def test_render_pcm_concatenates_frames_and_closes_stream():
    tts = _FakeTTS(chunks=[b"\x01\x02", b"\x03\x04"])
    pcm, sr, ch = asyncio.run(vc.render_pcm(tts, "hi"))

    assert pcm == b"\x01\x02\x03\x04"
    assert (sr, ch) == (24000, 1)
    assert tts.streams[0].closed, "synthesize() stream must be closed"


def test_render_pcm_closes_stream_even_when_synthesis_fails():
    tts = _FakeTTS(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(vc.render_pcm(tts, "hi"))
    assert tts.streams[0].closed


def test_render_uses_the_streaming_api_when_the_tts_supports_it():
    """Regression: rendering through the unary synthesize() gets
    `400 Unsupported audio encoding` from gemini-2.5-flash-tts. The live
    session speaks via stream(); pre-rendering has to use the same path."""
    tts = _FakeStreamingTTS(chunks=[b"\x01\x02", b"\x03\x04"])
    pcm, sr, ch = asyncio.run(vc.render_pcm(tts, "Ready, sir."))

    assert pcm == b"\x01\x02\x03\x04"
    assert (sr, ch) == (24000, 1)


def test_streaming_render_sends_the_text_then_closes_the_input():
    """Without flush()/end_input() the provider waits for more text forever —
    that's the 120s hang this replaced."""
    tts = _FakeStreamingTTS()
    asyncio.run(vc.render_pcm(tts, "Ready, sir."))

    s = tts.streams[0]
    assert s.pushed == ["Ready, sir."]
    assert s.flushed and s.input_ended
    assert s.closed


def test_streaming_render_closes_the_stream_when_synthesis_fails():
    tts = _FakeStreamingTTS(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(vc.render_pcm(tts, "hi"))
    assert tts.streams[0].closed


# --- frames / replay -------------------------------------------------------

def test_to_frames_preserves_every_sample():
    pcm = b"\x00\x01" * 24000  # 1s of 16-bit mono @24k
    frames = vc.to_frames(pcm, 24000, 1, chunk_ms=20)

    assert len(frames) == 50
    assert sum(f.samples_per_channel for f in frames) == 24000
    assert b"".join(bytes(f.data) for f in frames) == pcm


def test_to_frames_keeps_a_ragged_tail():
    """A line whose length isn't a whole number of chunks must not lose its end."""
    pcm = b"\x00\x01" * 1000  # 1000 samples; 20ms @24k = 480 → 480/480/40
    frames = vc.to_frames(pcm, 24000, 1, chunk_ms=20)

    assert [f.samples_per_channel for f in frames] == [480, 480, 40]
    assert b"".join(bytes(f.data) for f in frames) == pcm


def test_audio_stream_is_replayable():
    """say() is called on every wake — the same cached line must play twice."""
    frames = vc.to_frames(b"\x00\x01" * 960, 24000, 1)

    async def _drain():
        first = [f async for f in vc.audio_stream(frames)]
        second = [f async for f in vc.audio_stream(frames)]
        return first, second

    first, second = asyncio.run(_drain())
    assert len(first) == len(second) > 0
    assert [bytes(f.data) for f in first] == [bytes(f.data) for f in second]


# --- PrerenderedLines ------------------------------------------------------

def test_ensure_renders_each_line_once_and_serves_from_disk(tmp_path):
    lines = ["Ready when you are, sir.", "All set, sir."]

    warm = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    assert asyncio.run(warm.ensure(_FakeTTS(), lines)) == 2

    # A second process starting up must hit the disk cache, never the network.
    cold = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    assert asyncio.run(cold.ensure(_ExplodingTTS(), lines)) == 2
    assert cold.get(lines[0]) is not None


def test_ensure_skips_relines_already_in_memory(tmp_path):
    lines = ["Ready when you are, sir."]
    p = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    tts = _FakeTTS()

    asyncio.run(p.ensure(tts, lines))
    asyncio.run(p.ensure(tts, lines))

    assert tts.calls == lines, "a warm line must not be re-synthesised"


def test_a_failed_render_leaves_no_cache_file_and_no_line(tmp_path):
    """Partial audio must never be cached — it would be replayed forever."""
    p = vc.PrerenderedLines("Charon", cache_dir=tmp_path)

    assert asyncio.run(p.ensure(_FakeTTS(fail=True), ["Ready, sir."])) == 0
    assert p.get("Ready, sir.") is None
    assert list(tmp_path.glob("*.wav")) == []


def test_get_returns_none_for_an_unrendered_line(tmp_path):
    """The caller uses None to fall back to the live LLM+TTS path."""
    p = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    asyncio.run(p.ensure(_FakeTTS(), ["Ready, sir."]))

    assert p.get("Something else entirely") is None


def test_pick_only_ever_returns_a_rendered_line(tmp_path):
    lines = ["Ready, sir.", "All set, sir."]
    p = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    asyncio.run(p.ensure(_FakeTTS(), lines))

    for _ in range(20):
        text, frames = p.pick()
        assert text in lines
        assert frames


def test_pick_returns_none_when_nothing_is_rendered(tmp_path):
    p = vc.PrerenderedLines("Charon", cache_dir=tmp_path)
    assert p.pick() is None


def test_a_voice_change_invalidates_the_cache(tmp_path):
    """Switching TTS voice must not replay the old voice's audio."""
    line = "Ready, sir."
    asyncio.run(vc.PrerenderedLines("Charon", cache_dir=tmp_path).ensure(_FakeTTS(), [line]))

    puck = vc.PrerenderedLines("Puck", cache_dir=tmp_path)
    assert asyncio.run(puck.ensure(_ExplodingTTS(), [line])) == 0
    assert puck.get(line) is None
