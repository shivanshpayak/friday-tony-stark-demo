"""The wake acknowledgement must not cost an LLM call.

Every wake ran session.generate_reply(READY_LINE_INSTRUCTIONS) — a Gemini
round-trip to improvise one of three phrases the prompt already spelled out,
then a Google TTS synthesis of the result. The mic only opens once that line
finishes playing, so it *was* the wake→listening latency: 3.43-5.30s across the
activations in logs/friday.log on 2026-09-22 (median 3.76s).

Now a rendered line is replayed instead. The live path stays as the fallback,
so a cold cache or a recovery activation behaves exactly as it did before.
"""
import asyncio

import agent_friday
from friday.voice_cache import PrerenderedLines


class _FakeSession:
    """Records which path was taken, and in what order."""

    def __init__(self):
        self.said = []
        self.generated = []

    async def say(self, text, audio=None, **kw):
        # say() is what skips the LLM+TTS; drain the audio as the real one does.
        frames = [f async for f in audio] if audio is not None else []
        self.said.append((text, len(frames)))

    async def generate_reply(self, *, instructions=None, tool_choice=None, **kw):
        self.generated.append(instructions)


def _lines(texts=("Ready when you are, sir.",)):
    """A cache pre-populated without touching TTS or the disk."""
    p = PrerenderedLines("test-voice")
    p._frames = {t: [object(), object()] for t in texts}
    return p


def test_a_normal_wake_replays_audio_and_never_calls_the_llm():
    session = _FakeSession()
    path = asyncio.run(
        agent_friday._speak_ready_line(session, _lines(), recovered=False)
    )

    assert path == "pre-rendered"
    assert session.generated == [], "the wake path must not hit the LLM"
    assert len(session.said) == 1


def test_the_replayed_line_carries_its_cached_frames():
    """say(text, audio=...) with no frames would be silence, not a greeting."""
    session = _FakeSession()
    asyncio.run(agent_friday._speak_ready_line(session, _lines(), recovered=False))

    text, frame_count = session.said[0]
    assert text == "Ready when you are, sir."
    assert frame_count == 2


def test_the_spoken_line_is_always_one_that_was_rendered():
    session = _FakeSession()
    texts = ("Ready when you are, sir.", "Standing by, sir.")
    for _ in range(10):
        asyncio.run(agent_friday._speak_ready_line(session, _lines(texts), recovered=False))

    assert {t for t, _ in session.said} <= set(texts)


def test_a_cold_cache_falls_back_to_the_live_path():
    """First-ever boot, or a render failure — behaviour must not regress."""
    session = _FakeSession()
    path = asyncio.run(
        agent_friday._speak_ready_line(session, PrerenderedLines("v"), recovered=False)
    )

    assert path == "live"
    assert session.said == []
    assert session.generated == [agent_friday._READY_LINE_INSTRUCTIONS]


def test_recovery_still_improvises_its_line():
    """The recovery line acknowledges a drop that just happened — a fixed
    phrase would be wrong, so it must keep using the LLM even with a warm cache."""
    session = _FakeSession()
    path = asyncio.run(
        agent_friday._speak_ready_line(session, _lines(), recovered=True)
    )

    assert path == "live"
    assert session.said == []
    assert session.generated == [agent_friday.RECOVERY_LINE_INSTRUCTIONS]


def test_ready_responses_are_configured_and_short():
    """The mic waits for this line to finish playing — keep them brief, and in
    FRIDAY's voice ('sir', never the user's name)."""
    from friday.config import READY_RESPONSES

    assert READY_RESPONSES
    for line in READY_RESPONSES:
        assert len(line) <= 60, f"too long to sit on the wake path: {line!r}"
        assert "sir" in line.lower()


def test_ready_responses_are_single_sentence():
    """Gemini TTS's streaming plugin splits multi-sentence input into segments
    and then fails the render with "no audio frames were pushed" — verified
    against the live API on 2026-09-22 with "All set, sir. What can I do for
    you?", which failed with and without an explicit flush(). Such a line would
    never cache, and would quietly fall back to the slow live path forever."""
    from friday.config import READY_RESPONSES

    for line in READY_RESPONSES:
        body = line.rstrip(".!?")
        assert not any(c in body for c in ".!?"), (
            f"multi-sentence lines never render: {line!r}"
        )
