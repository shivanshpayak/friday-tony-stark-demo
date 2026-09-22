"""Dismissal ends the session when the sign-off has actually finished playing.

It used to be a fixed asyncio.sleep(6.0) after the dismissal phrase: a short
"Standing watch, sir." finished in ~3-4s, but the wake word stayed off for the
full 6s (plus the launcher's 1.5s), and a sign-off longer than 6s was cut off
when the session ended.
"""
import asyncio
import time
from pathlib import Path

import agent_friday


def _run(speaking_after=None, finished_after=None, start_timeout=1.0, max_speech=1.0):
    async def scenario():
        speaking, finished = asyncio.Event(), asyncio.Event()

        async def fire(event, delay):
            await asyncio.sleep(delay)
            event.set()

        if speaking_after is not None:
            asyncio.create_task(fire(speaking, speaking_after))
        if finished_after is not None:
            asyncio.create_task(fire(finished, finished_after))
        t0 = time.monotonic()
        outcome = await agent_friday._wait_for_signoff(
            speaking, finished, start_timeout=start_timeout, max_speech=max_speech)
        return outcome, time.monotonic() - t0

    return asyncio.run(scenario())


def test_returns_as_soon_as_the_signoff_finishes():
    outcome, elapsed = _run(speaking_after=0.05, finished_after=0.15)
    assert outcome == "played"
    assert elapsed < 0.5, "must not wait out the timeouts once playback ends"


def test_gives_up_when_no_reply_starts():
    outcome, elapsed = _run(start_timeout=0.2)
    assert outcome == "no reply"
    assert 0.15 < elapsed < 0.6


def test_caps_a_signoff_that_never_finishes():
    outcome, elapsed = _run(speaking_after=0.05, max_speech=0.2)
    assert outcome == "still speaking"
    assert elapsed < 0.6


def test_sign_off_uses_playout_wait_not_a_fixed_sleep():
    """Source guard: the sign-off must be driven by agent speaking state."""
    src = Path(agent_friday.__file__).read_text(encoding="utf-8")
    sign_off = src[src.index("async def _sign_off"):src.index("asyncio.create_task(_sign_off())")]
    assert "_wait_for_signoff(" in sign_off
    assert "asyncio.sleep(6.0)" not in sign_off
    assert 'session.on("agent_state_changed")' in src
