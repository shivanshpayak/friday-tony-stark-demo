"""Guards the mic-gating order that prevents phantom transcripts at boot.

The bug: `session.input.set_audio_enabled(False)` was called AFTER
`await session.start(...)`. During `start()`, LiveKit's console backend creates
a `ConsoleAudioInput` that is attached by default and immediately begins
queueing mic frames into an unbounded channel. `AgentSession._forward_audio_task`
drains that channel and pushes every frame to the activity — it never consults
`input.audio_enabled`. So the frames captured in the window between device start
and our gate call still reached Deepgram, which endpointed the burst and emitted
a hallucinated transcript ("Mhmm."), which the session then treated as a real
user turn and sent to Gemini — four failing calls, before the user had said
anything.

The fix: gate BEFORE `start()`. `AgentInput.audio`'s setter checks
`_audio_enabled` when the stream is assigned and calls `on_detached()` if it is
False, so `ConsoleAudioInput._attached` is False from the outset and
`push_frame` drops frames instead of queueing them. No backlog can form.
"""
import asyncio
import re
from pathlib import Path

from livekit import rtc
from livekit.agents.cli.cli import ConsoleAudioInput
from livekit.agents.voice.io import AgentInput

_AGENT_SRC = Path(__file__).parents[1] / "agent_friday.py"


def _make_input() -> AgentInput:
    return AgentInput(video_changed=lambda: None, audio_changed=lambda: None)


def _make_frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x00\x00" * 240,
        sample_rate=24000,
        num_channels=1,
        samples_per_channel=240,
    )


async def _next_frame_or_none(stream: ConsoleAudioInput):
    """Pull one frame, or None if the channel is empty."""
    try:
        return await asyncio.wait_for(stream.__anext__(), timeout=0.15)
    except asyncio.TimeoutError:
        return None


def test_gating_after_stream_assignment_leaks_frames():
    """Documents the BUG. If this ever starts passing, LiveKit changed and the
    ordering fix below may no longer be necessary."""

    async def scenario():
        inp = _make_input()
        stream = ConsoleAudioInput(asyncio.get_running_loop())
        inp.audio = stream            # attached by default
        stream.push_frame(_make_frame())   # mic callback during start()
        inp.set_audio_enabled(False)       # our gate — too late
        return await _next_frame_or_none(stream)

    assert asyncio.run(scenario()) is not None, (
        "expected the pre-gate frame to still be queued — this is the bug the "
        "ordering fix works around"
    )


def test_gating_before_stream_assignment_drops_frames():
    """The invariant the fix depends on."""

    async def scenario():
        inp = _make_input()
        inp.set_audio_enabled(False)       # gate FIRST
        stream = ConsoleAudioInput(asyncio.get_running_loop())
        inp.audio = stream                 # setter honours _audio_enabled
        stream.push_frame(_make_frame())
        return await _next_frame_or_none(stream)

    assert asyncio.run(scenario()) is None, (
        "gating before stream assignment must drop frames; LiveKit's "
        "AgentInput.audio setter no longer honours _audio_enabled"
    )


def test_reenabling_after_early_gate_still_delivers_audio():
    """Activation must still work — the gate has to be reversible."""

    async def scenario():
        inp = _make_input()
        inp.set_audio_enabled(False)
        stream = ConsoleAudioInput(asyncio.get_running_loop())
        inp.audio = stream
        inp.set_audio_enabled(True)
        stream.push_frame(_make_frame())
        return await _next_frame_or_none(stream)

    assert asyncio.run(scenario()) is not None


def test_agent_gates_audio_before_starting_the_session():
    """Ordering guard: a tidy-up that moves the gate back below session.start()
    silently reintroduces the phantom transcript, with no test failure anywhere
    else. Keep them in this order.

    Matches `_pre_gate_mic(` rather than a bare `set_audio_enabled(False)` on
    purpose: the latter also appears inside the STT watchdog's nested recovery
    function, which sits EARLIER in the file but runs long after start — so a
    first-occurrence match on it makes this test pass vacuously.
    """
    src = _AGENT_SRC.read_text(encoding="utf-8")

    calls = [m.start() for m in re.finditer(r"^\s+_pre_gate_mic\(session\)", src, re.M)]
    start = re.search(r"await session\.start\(", src)

    assert len(calls) == 1, (
        f"expected exactly one _pre_gate_mic(session) call site, found {len(calls)}"
    )
    assert start is not None, "await session.start( not found in agent_friday.py"
    assert calls[0] < start.start(), (
        "agent_friday.py must call _pre_gate_mic(session) BEFORE "
        "await session.start(...) — gating afterwards lets the console audio "
        "backlog leak into STT and fire a phantom LLM turn at boot"
    )
