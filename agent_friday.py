"""
JARVIS – Voice Agent
====================
Iron Man-style voice assistant powered by LiveKit Agents SDK.
All config lives in friday/config.py, providers in friday/providers.py.

Run:
  friday_start             – full launcher with wake word
  python agent_friday.py console  – text-only console mode
"""

import asyncio
import os
import re
import random
import sys

import numpy as np

# ---------------------------------------------------------------------------
# Mic selection — apply BEFORE livekit imports, so sounddevice picks it up.
# Set SESSION_MIC=<name substring> in your .env (e.g. "USB Microphone") to
# route the agent's mic capture to a specific device. Empty = system default.
# ---------------------------------------------------------------------------
def _apply_session_mic() -> None:
    name = os.getenv("SESSION_MIC", "").strip()
    if not name:
        return
    try:
        import sounddevice as sd
        needle = name.lower()
        for idx, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) <= 0:
                continue
            if needle in info["name"].lower():
                # Tuple is (input, output); leave output as default.
                sd.default.device = (idx, sd.default.device[1])
                print(f"SESSION_MIC resolved: {info['name']!r} (idx={idx})",
                      flush=True)
                return
        print(f"SESSION_MIC {name!r} not found — using system default", flush=True)
    except Exception as e:
        print(f"SESSION_MIC setup failed: {e}", flush=True)

_apply_session_mic()

from pathlib import Path

from livekit import rtc
from livekit.agents import (
    JobContext, WorkerOptions, cli,
    llm as lk_llm, stt, TurnHandlingOptions,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.turn import InterruptionOptions, EndpointingOptions
from livekit.plugins import silero

from friday.config import (
    SYSTEM_PROMPT, DISMISSAL_PHRASES, SLEEP_RESPONSES,
    STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    MAX_HISTORY_ITEMS, logger,
)
from friday.providers import build_stt, build_llm, build_tts
from friday.routing import LocalDomainToolPool
from friday.speaker_gate import get_speaker_gate

# Repo root — used to locate server.py when spawning the MCP subprocess.
_REPO_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Tool-call leakage scrubber (only needed for llama models via Groq/Ollama)
# ---------------------------------------------------------------------------

_TOOL_LEAK_RE = re.compile(
    r"""
    (?:
        <\|python_tag\|>
      | <\|?function[^>|]*\|?>
      | </function>
      | <tool_call>.*?(?:</tool_call>|$)
      | function\s*=\s*["'<]?\w+["'>]?
      | \{\s*["']name["']\s*:\s*["'][^"']+["'].*?\}
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


class _ToolLeakScrubber:
    """Strips tool-call syntax leaked into assistant text by llama models."""

    SAFE_TAIL = 64

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._pending += text
        self._pending = _TOOL_LEAK_RE.sub("", self._pending)
        if len(self._pending) > self.SAFE_TAIL:
            out = self._pending[: -self.SAFE_TAIL]
            self._pending = self._pending[-self.SAFE_TAIL :]
            return out
        return ""

    def flush(self) -> str:
        out = _TOOL_LEAK_RE.sub("", self._pending)
        self._pending = ""
        return out


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FridayAgent(Agent):
    """F.R.I.D.A.Y. voice agent. Tools come from the local MCP server
    (see server.py + friday/tools/). This class keeps voice-specific
    concerns: speaker gating, history trimming, streaming signals, greeting."""

    def __init__(self, stt, llm, tts, vad=None) -> None:
        from friday.tools.memory import get_memories_prompt
        memories = get_memories_prompt()
        full_prompt = SYSTEM_PROMPT + ("\n\n" + memories if memories else "")
        super().__init__(
            instructions=full_prompt,
            stt=stt, llm=llm, tts=tts,
            vad=vad or silero.VAD.load(
                # Bumped from 0.92 / 0.5 to filter out background chatter,
                # typing, distant voices, brief noises during a session.
                activation_threshold=0.6,
                min_speech_duration=0.1,
                min_silence_duration=0.5,
            ),
        )

    # -- Speaker-gated STT ------------------------------------------------

    async def stt_node(self, audio, model_settings):
        """Only yield transcripts from the enrolled voice."""
        gate = get_speaker_gate()
        if not gate.enabled:
            async for ev in Agent.default.stt_node(self, audio, model_settings):
                yield ev
            return

        audio_buf: list[rtc.AudioFrame] = []
        sample_rate = 16000

        async def _buffered_audio():
            nonlocal sample_rate
            async for frame in audio:
                audio_buf.append(frame)
                sample_rate = frame.sample_rate
                yield frame

        async for ev in Agent.default.stt_node(self, _buffered_audio(), model_settings):
            if not isinstance(ev, stt.SpeechEvent):
                yield ev
                continue
            if ev.type not in (
                stt.SpeechEventType.FINAL_TRANSCRIPT,
                stt.SpeechEventType.INTERIM_TRANSCRIPT,
            ):
                yield ev
                continue

            if audio_buf:
                pcm = np.concatenate(
                    [np.frombuffer(f.data, dtype=np.int16) for f in audio_buf]
                )
                is_user = gate.verify(pcm, sample_rate)
            else:
                is_user = True

            if is_user:
                yield ev
            else:
                text = ev.alternatives[0].text if ev.alternatives else ""
                logger.info("Suppressed non-user transcript: %r (type=%s)",
                            text[:60], ev.type.name)
            audio_buf.clear()

    # -- LLM with history trimming + optional scrubber --------------------

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Trim history and optionally scrub tool-call leaks (llama only).

        Tool surface: the framework hands us the full toolset every turn
        (no domain filtering). Frontier LLMs handle ~100 tools well; revisit
        with semantic retrieval if/when we cross that threshold.
        """
        if MAX_HISTORY_ITEMS and len(chat_ctx.items) > MAX_HISTORY_ITEMS:
            chat_ctx = chat_ctx.truncate(max_items=MAX_HISTORY_ITEMS)

        # Signal the launcher that we're thinking
        print("PROCESSING", flush=True)

        use_scrubber = LLM_PROVIDER in ("groq", "ollama")
        scrubber = _ToolLeakScrubber() if use_scrubber else None
        first_content = True

        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            # Signal when first real content arrives (TTS will start speaking)
            if (
                first_content
                and isinstance(chunk, lk_llm.ChatChunk)
                and chunk.delta is not None
                and chunk.delta.content
            ):
                print("SPEAKING", flush=True)
                first_content = False

            if (
                scrubber
                and isinstance(chunk, lk_llm.ChatChunk)
                and chunk.delta is not None
                and chunk.delta.content
            ):
                cleaned = scrubber.feed(chunk.delta.content)
                if cleaned or chunk.delta.tool_calls:
                    new_delta = chunk.delta.model_copy(
                        update={"content": cleaned if cleaned else None}
                    )
                    yield chunk.model_copy(update={"delta": new_delta})
            else:
                yield chunk

        if scrubber:
            tail = scrubber.flush()
            if tail:
                yield lk_llm.ChatChunk(
                    id="scrub-tail",
                    delta=lk_llm.ChoiceDelta(role="assistant", content=tail),
                )

    # -- Greeting ---------------------------------------------------------

    async def on_enter(self) -> None:
        # Intentionally silent. Greetings are fired from the activation loop
        # in entrypoint() on each START — this lets us pre-start the session
        # during boot (paying VAD load + connection setup up-front) without
        # the agent speaking into an empty room. Result: first "hey friday"
        # feels as snappy as re-activation.
        logger.info("on_enter — session attached (greeting deferred to activation)")


# ---------------------------------------------------------------------------
# Session entrypoint (stdin/stdout protocol with friday_launcher.py)
# ---------------------------------------------------------------------------

def _endpointing_delay() -> float:
    return {"sarvam": 0.07, "whisper": 0.3}.get(STT_PROVIDER, 0.1)


async def _stdin_dispatch_loop(session, cmd_queue: asyncio.Queue) -> None:
    """Read stdin lines and route them.

    START / QUIT go to the activation queue (consumed by the activation
    loop). INTERRUPT is handled inline by calling `session.interrupt()`
    so it can fire mid-turn without waiting for the activation loop to
    finish whatever it's currently awaiting.
    """
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await cmd_queue.put("QUIT")
            return
        cmd = line.strip().upper()
        if cmd == "INTERRUPT":
            try:
                # force=True bypasses the per-speech `_allow_interruptions`
                # gate, which is False because we set InterruptionOptions(
                # enabled=False) to disable VERBAL interruption. Manual
                # keybind interrupts must override that gate.
                session.interrupt(force=True)
                logger.info("INTERRUPT received — session.interrupt(force=True) called")
            except Exception as e:
                logger.warning("session.interrupt() failed: %s", e)
        elif cmd in ("START", "QUIT"):
            await cmd_queue.put(cmd)


_READY_LINE_INSTRUCTIONS = (
    "You have just finished waking up and are now ready to help. "
    "Reply with one short, professional sentence that invites the user's request. "
    "Examples: 'Done, sir. What can I help you with?', 'All set, sir. What can I do for you?', 'Ready when you are, sir.' "
    "Never use his name. Do NOT mention time, boot steps, setup, or tools. Do NOT call any tools."
)


async def _fire_greeting(session) -> None:
    """Generate the activation greeting on the already-running session."""
    try:
        await session.generate_reply(
            instructions=_READY_LINE_INSTRUCTIONS,
            tool_choice="none",
        )
        logger.info("Greeting completed")
    except Exception as e:
        logger.warning("Greeting failed: %s", e)


def _refresh_stt_streams(stt_inst) -> None:
    """Force Sarvam's live STT streams to reconnect before each activation."""
    if STT_PROVIDER != "sarvam":
        return

    streams = list(getattr(stt_inst, "_streams", ()))
    if not streams:
        logger.info("No active Sarvam STT streams found to refresh")
        return

    refreshed = 0
    for stream in streams:
        update_options = getattr(stream, "update_options", None)
        if not callable(update_options):
            continue
        try:
            update_options(language="en-IN", model="saaras:v3", mode="transcribe")
            refreshed += 1
        except Exception as e:
            logger.warning("Could not refresh Sarvam STT stream: %s", e)

    if refreshed:
        logger.info("Refreshed %d Sarvam STT stream(s) for activation", refreshed)


async def entrypoint(ctx: JobContext) -> None:
    """Boot once, pre-start the session, then loop activations.

    Key timing choice: the LiveKit session is started BEFORE we announce
    FRIDAY_READY to the launcher. That front-loads the VAD load +
    room connection cost (~2s) into boot, so the first "hey friday"
    after launch is as fast as a re-activation. We keep the mic
    disabled on the session until the user actually wakes the agent,
    so no transcripts reach the LLM during the idle wait.

    Console mode's virtual room doesn't recover after session.aclose(),
    so we keep ONE session alive for the entire process lifetime.
    On dismissal we just signal the launcher (SESSION_DONE) without
    tearing down the session. On re-activation we re-enable audio
    and generate a fresh greeting on the same live session.
    """
    logger.info("JARVIS online — room: %s | STT=%s | LLM=%s | TTS=%s",
                ctx.room.name, STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER)

    # Warm-up: build providers + speaker gate in parallel. Each factory
    # is a plain synchronous constructor call with no shared state, so
    # running them through the default thread pool is safe and shaves a
    # second or two off boot time.
    loop = asyncio.get_event_loop()
    stt_inst, llm_inst, tts_inst, _ = await asyncio.gather(
        loop.run_in_executor(None, build_stt),
        loop.run_in_executor(None, build_llm),
        loop.run_in_executor(None, build_tts),
        loop.run_in_executor(None, get_speaker_gate),
    )
    logger.info("Providers + speaker gate warm — preparing session")

    # Keep a small always-on core tool surface warm, and route heavier domains
    # in only when the current request needs them.
    tool_pool = LocalDomainToolPool(repo_root=_REPO_ROOT)
    core_toolset = tool_pool.get_toolset("core")
    logger.info("Domain tool pool prepared (%s)", _REPO_ROOT / "server.py")

    # Pre-warm the core toolset in the background so the first tool call is instant
    asyncio.create_task(core_toolset.setup())

    session = AgentSession(
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(
                min_delay=_endpointing_delay(),
                max_delay=0.8,
            ),
            # Verbal interruption disabled — only the launcher's PTT keybind
            # interrupts an in-flight turn (via INTERRUPT stdin command below).
            interruption=InterruptionOptions(enabled=False),
        ),
        tools=[core_toolset],
        max_tool_steps=3,
    )

    # -----------------------------------------------------------------------
    # Phase 0.5: Task Orchestration Setup
    # -----------------------------------------------------------------------
    from friday.tasking import register_toolset, start_worker, set_completion_callback
    register_toolset(core_toolset)
    start_worker()
    
    async def _on_task_finished(task):
        try:
            logger.info(f"Task {task.task_id} finished ({task.status}).")
            summary = task.final_summary or ""

            if summary.startswith("TOKEN_LIMIT:"):
                # Claude ran out of tokens — offer alternatives
                await session.generate_reply(
                    instructions=(
                        "Claude ran out of tokens or hit a rate limit while working "
                        "on a task. Inform the user briefly and ask if they'd like "
                        "you to try it yourself instead, or try again later. "
                        "Keep it to one or two sentences. "
                        "Example: 'Claude hit its token limit on that one, sir. "
                        "Want me to try it myself?'"
                    ),
                    tool_choice="none",
                )
            elif summary.startswith("TIMEOUT:"):
                await session.generate_reply(
                    instructions=(
                        "Claude took too long on a background task and was stopped. "
                        "Inform the user briefly and ask if they'd like you to try "
                        "it yourself. One sentence."
                    ),
                    tool_choice="none",
                )
            elif task.status == "failed":
                await session.generate_reply(
                    instructions=(
                        f"A background task failed. Error: '{summary}'\n\n"
                        f"Inform the user briefly in one sentence that it didn't work. "
                        f"Don't read technical errors. Ask if they want to try another way."
                    ),
                    tool_choice="none",
                )
            else:
                await session.generate_reply(
                    instructions=(
                        f"A background task just completed. "
                        f"Here is the raw result: '{summary}'\n\n"
                        f"Summarize this in ONE short sentence to the user. "
                        f"Do not read filenames, paths, technical details, or feature lists. "
                        f"Just confirm what was done. Example: 'The dice roller is done, sir.'"
                    ),
                    tool_choice="none",
                )
        except Exception as e:
            logger.error(f"Task completion callback failed: {e}")
            
    set_completion_callback(_on_task_finished)

    # -----------------------------------------------------------------------
    # Scheduler — fires due timers and reminders via TTS
    # -----------------------------------------------------------------------
    from friday.scheduling import set_fire_callback, start_scheduler

    async def _on_scheduled_fire(item):
        try:
            logger.info("Scheduled %s fired (%s): %s", item.kind, item.id, item.message)
            await session.generate_reply(
                instructions=(
                    f"A scheduled {item.kind} just went off. "
                    f"Speak this to the user verbatim, then stop: '{item.message}'"
                ),
                tool_choice="none",
            )
        except Exception as e:
            logger.error("Scheduled fire callback failed: %s", e)

    set_fire_callback(_on_scheduled_fire)
    start_scheduler()

    # Dismissal event — set when the user says goodbye, cleared on re-activation.
    # Start in "dismissed" state so the activation loop's first iteration
    # waits for a START command instead of immediately looping.
    dismissed = asyncio.Event()
    dismissed.set()

    _signing_off = False   # guard against interim+final double-fire

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev):
        nonlocal _signing_off
        text = (ev.transcript or "").lower().strip()
        if dismissed.is_set() or _signing_off:
            return
        if any(phrase in text for phrase in DISMISSAL_PHRASES):
            _signing_off = True
            logger.info("Dismissal detected: %r", text)

            async def _sign_off():
                nonlocal _signing_off
                # The LLM already knows to say a casual sign-off for dismissal
                # phrases (via the system prompt). Don't generate a second one —
                # just wait for the natural response to play out, then end session.
                await asyncio.sleep(6.0)
                logger.info("Sign-off complete")
                _signing_off = False
                dismissed.set()

            asyncio.create_task(_sign_off())

    # Pre-start the session so VAD + room connection is warm before the
    # first "hey friday". Silence the mic immediately — we don't want
    # transcripts landing in the LLM before the user actually wakes it.
    await session.start(
        agent=FridayAgent(stt=stt_inst, llm=llm_inst, tts=tts_inst),
        room=ctx.room,
    )
    try:
        session.input.set_audio_enabled(False)
    except Exception as e:
        logger.warning("Failed to pre-disable audio input: %s", e)
    logger.info("Session pre-warmed, audio gated off — awaiting first START")
    print("FRIDAY_READY", flush=True)

    # ---- Stdin dispatcher: handles START/QUIT (queued) and INTERRUPT (inline)
    cmd_queue: asyncio.Queue = asyncio.Queue()
    asyncio.create_task(_stdin_dispatch_loop(session, cmd_queue))

    # ---- Activation loop (first START + every subsequent one use the same path) ----
    while True:
        logger.info("Waiting for START command on stdin…")
        cmd = await cmd_queue.get()
        if cmd != "START":
            logger.info("Received %r — shutting down", cmd or "EOF")
            break

        # Re-activate: clear dismissal, refresh STT, then generate the ready line
        _signing_off = False
        dismissed.clear()
        print("SESSION_STARTED", flush=True)

        try:
            _refresh_stt_streams(stt_inst)
        except Exception as e:
            logger.warning("STT refresh failed before activation: %s", e)

        try:
            await session.generate_reply(
                instructions=_READY_LINE_INSTRUCTIONS,
                tool_choice="none",
            )
            logger.info("Re-activation ready line completed")
        except Exception as e:
            logger.warning("Re-activation ready line failed: %s", e)

        # Enable mic after the ready line so the agent doesn't hear itself.
        try:
            session.input.set_audio_enabled(True)
            logger.info("Audio input enabled — listening for user speech")
            print("SESSION_LISTENING", flush=True)
        except Exception as e:
            logger.warning("Failed to enable audio input: %s", e)

        # Stay active until user dismisses ("that'll be all", etc.)
        await dismissed.wait()
        logger.info("Session dismissed — gating mic")
        try:
            session.input.set_audio_enabled(False)
        except Exception:
            pass
        print("SESSION_DONE", flush=True)

    # Clean up
    try:
        await session.aclose()
    except Exception:
        pass
    try:
        await tool_pool.aclose()
    except Exception:
        pass
    ctx.shutdown("stdin closed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

def dev():
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    main()

if __name__ == "__main__":
    main()
