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


# ---------------------------------------------------------------------------
# Windows ProactorEventLoop + anyio "happy eyeballs" hang.
# anyio.connect_tcp() races multiple socket connects (IPv4/IPv6) and cancels
# the losers. On Windows' ProactorEventLoop, cancelling an in-flight overlapped
# TCP connect wedges the task forever in "cancelling" state (its _OverlappedFuture
# is cancelled but never completes), so anyio's task group never exits and the
# whole HTTP connection hangs. This intermittently froze the first Gemini LLM
# call during the greeting (confirmed via asyncio task-stack dump). httpcore
# calls anyio.connect_tcp WITHOUT passing happy_eyeballs_delay, so forcing a huge
# default makes connects sequential (one attempt at a time) — no racing, no
# cancellation, no hang. Negligible cost: connecting to Google always succeeds
# on the first address.
def _patch_anyio_happy_eyeballs() -> None:
    try:
        import functools
        import anyio
        _orig_connect_tcp = anyio.connect_tcp
        if getattr(_orig_connect_tcp, "_friday_seq_patched", False):
            return

        @functools.wraps(_orig_connect_tcp)
        async def _sequential_connect_tcp(*args, **kwargs):
            kwargs.setdefault("happy_eyeballs_delay", 86400.0)  # effectively disable racing
            return await _orig_connect_tcp(*args, **kwargs)

        _sequential_connect_tcp._friday_seq_patched = True
        anyio.connect_tcp = _sequential_connect_tcp
    except Exception as e:
        print(f"anyio happy-eyeballs patch failed: {e}", flush=True)

# Toggle to A/B test whether these network patches are implicated in an issue.
_DISABLE_NET_PATCHES = os.getenv("FRIDAY_DISABLE_NET_PATCHES") == "1"
if not _DISABLE_NET_PATCHES:
    _patch_anyio_happy_eyeballs()


# ---------------------------------------------------------------------------
# Force IPv4-only name resolution.
# The ProactorEventLoop connect-cancellation wedge (see above) is only ever
# triggered because connections race IPv4 vs IPv6 and cancel the loser. All of
# FRIDAY's providers (Gemini, Google TTS, Deepgram, Groq) are reachable over
# IPv4, and each resolves to a single IPv4 address here — so restricting name
# resolution to IPv4 means there is nothing to race, nothing to cancel, and no
# stall on a first-tried IPv6 address when the event loop is under load. We only
# override the default (unspecified-family) lookups, and fall back to the normal
# resolver if a host has no IPv4 record, so IPv6-only hosts still work.
def _force_ipv4_resolution() -> None:
    try:
        import socket
        _orig_getaddrinfo = socket.getaddrinfo
        if getattr(_orig_getaddrinfo, "_friday_ipv4_only", False):
            return

        def _ipv4_only(host, port, family=0, *args, **kwargs):
            if family == 0:
                try:
                    res = _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
                    if res:
                        return res
                except socket.gaierror:
                    pass
                return _orig_getaddrinfo(host, port, 0, *args, **kwargs)
            return _orig_getaddrinfo(host, port, family, *args, **kwargs)

        _ipv4_only._friday_ipv4_only = True
        socket.getaddrinfo = _ipv4_only
    except Exception as e:
        print(f"IPv4-only resolution patch failed: {e}", flush=True)

if not _DISABLE_NET_PATCHES:
    _force_ipv4_resolution()


# ---------------------------------------------------------------------------
# Console APM: force all processing OFF (passthrough).
# livekit's console audio builds AudioProcessingModule(echo_cancellation=True,
# noise_suppression=True, high_pass_filter=True, auto_gain_control=True) and runs
# the mic through process_stream before the STT. Evidence strongly suggests this
# pipeline strips the user's voice down to the noise floor here (raw mic peaks
# ~7800, but frames reaching the STT peak ~26), so the STT receives silence and
# returns no transcripts. Force an all-off (passthrough) APM. FRIDAY's speaker
# gate already filters the agent's own voice, so console AEC buys us nothing.
# Set FRIDAY_KEEP_APM=1 to restore livekit's default APM.
def _force_console_apm_off() -> None:
    if os.getenv("FRIDAY_KEEP_APM") == "1":
        return
    try:
        from livekit import rtc
        _orig_apm = rtc.AudioProcessingModule
        if getattr(_orig_apm, "_friday_apm_off", False):
            return

        def _apm_off(*args, **kwargs):
            return _orig_apm(
                echo_cancellation=False,
                noise_suppression=False,
                high_pass_filter=False,
                auto_gain_control=False,
            )

        _apm_off._friday_apm_off = True
        rtc.AudioProcessingModule = _apm_off
        print("Console APM forced OFF (passthrough)", flush=True)
    except Exception as e:
        print(f"APM-off patch failed: {e}", flush=True)

_force_console_apm_off()


from pathlib import Path

from livekit import rtc
from livekit.agents import (
    JobContext, WorkerOptions, cli,
    llm as lk_llm, stt, TurnHandlingOptions,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.events import CloseReason  # noqa: F401  (documents the reason values)
from livekit.agents.voice.turn import InterruptionOptions, EndpointingOptions
from friday.webrtc_vad import WebRTCVAD

from friday.config import (
    SYSTEM_PROMPT, DISMISSAL_PHRASES, SLEEP_RESPONSES,
    STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    MAX_HISTORY_ITEMS, SESSION_SPEAKER_GATE_MAX_REJECTS, logger,
    RECOVERY_LINE_INSTRUCTIONS,
)
from friday.providers import build_stt, build_llm, build_tts, build_session_conn_options
from friday.routing import LocalDomainToolPool
from friday.speaker_gate import get_speaker_gate
from friday.recovery import is_fatal_close

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


# Whisper hallucinates these on silence/noise (training-set leakage from
# YouTube outros, subtitle files). Drop them before they reach the reply
# pipeline or count against the speaker gate's fail-open budget.
_WHISPER_HALLUCINATIONS = frozenset({
    "",
    "thank you",
    "thanks",
    "thanks for watching",
    "you",
    "bye",
    "goodbye",
    "amara.org",
})


def _is_whisper_hallucination(text: str) -> bool:
    normalized = text.strip().lower().rstrip(".!?,").strip()
    return normalized in _WHISPER_HALLUCINATIONS


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
        # WebRTC VAD: ~100× faster than Silero ONNX on this CPU. Silero kept
        # running "slower than realtime" even at 8 kHz, backing up audio
        # minutes deep until the agent looked frozen. WebRTC is noisier
        # (triggers on keystrokes), but the enrolled speaker gate filters
        # any spurious transcripts downstream.
        super().__init__(
            instructions=full_prompt,
            stt=stt, llm=llm, tts=tts,
            vad=vad or WebRTCVAD(
                aggressiveness=2,
                min_speech_frames=5,   # 100 ms minimum speech
                min_silence_frames=25, # 500 ms silence to end turn
            ),
        )
        self._gate_reject_streak = 0
        self._gate_fail_open = False

    # -- Speaker-gated STT ------------------------------------------------

    async def stt_node(self, audio, model_settings):
        """Only yield transcripts from the enrolled voice."""
        gate = get_speaker_gate()

        # Audio-flow probe: logs every 5s (≈500 frames at 100fps) so we can
        # see at a glance whether frames keep arriving from the mic. Also fires
        # an alarm if no frame has arrived in 3s — that's the signature of the
        # post-activation freeze.
        async def _probed(src):
            count = 0
            last_log_at = 0.0
            last_frame_at = 0.0
            loop = asyncio.get_event_loop()
            async def _no_frame_alarm():
                nonlocal last_frame_at
                while True:
                    await asyncio.sleep(1.0)
                    if last_frame_at > 0 and loop.time() - last_frame_at > 3.0:
                        logger.warning(
                            "stt_node: NO audio frames for %.1fs — mic feed wedged",
                            loop.time() - last_frame_at,
                        )
                        # only warn once per outage
                        last_frame_at = 0.0
            alarm_task = asyncio.create_task(_no_frame_alarm())
            try:
                async for f in src:
                    now = loop.time()
                    if last_frame_at == 0.0:
                        logger.info("stt_node: audio frames flowing (sr=%d)",
                                    f.sample_rate)
                    last_frame_at = now
                    count += 1
                    if now - last_log_at >= 5.0:
                        logger.info("stt_node: %d frames in last interval", count)
                        last_log_at = now
                        count = 0
                    yield f
            finally:
                alarm_task.cancel()

        if not gate.enabled or self._gate_fail_open:
            async for ev in Agent.default.stt_node(self, _probed(audio), model_settings):
                if (
                    isinstance(ev, stt.SpeechEvent)
                    and ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT
                    and ev.alternatives
                    and _is_whisper_hallucination(ev.alternatives[0].text)
                ):
                    logger.debug("Dropped Whisper hallucination: %r",
                                 ev.alternatives[0].text[:60])
                    continue
                yield ev
            return

        audio_buf: list[rtc.AudioFrame] = []
        sample_rate = 16000

        async def _buffered_audio():
            nonlocal sample_rate
            async for frame in _probed(audio):
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

            if (
                ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT
                and ev.alternatives
                and _is_whisper_hallucination(ev.alternatives[0].text)
            ):
                logger.debug("Dropped Whisper hallucination: %r",
                             ev.alternatives[0].text[:60])
                audio_buf.clear()
                continue

            if audio_buf:
                pcm = np.concatenate(
                    [np.frombuffer(f.data, dtype=np.int16) for f in audio_buf]
                )
                # gate.verify runs Resemblyzer's neural embedding extraction
                # which can take hundreds of ms. Pushing it to a thread keeps
                # the asyncio event loop free to log frames, fire watchdogs,
                # and process the next batch of audio.
                is_user = await asyncio.to_thread(gate.verify, pcm, sample_rate)
            else:
                is_user = True

            if is_user:
                self._gate_reject_streak = 0
                yield ev
            else:
                self._gate_reject_streak += 1
                text = ev.alternatives[0].text if ev.alternatives else ""
                logger.info("Suppressed non-user transcript: %r (type=%s)",
                            text[:60], ev.type.name)
                if self._gate_reject_streak >= SESSION_SPEAKER_GATE_MAX_REJECTS:
                    # Safety fallback: if the voice gate keeps rejecting in-session
                    # speech, fail open so the assistant remains usable.
                    self._gate_fail_open = True
                    logger.warning(
                        "Speaker gate rejected %d consecutive transcripts; "
                        "failing open for this activation.",
                        self._gate_reject_streak,
                    )
                    yield ev
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
        elif cmd in ("START", "START_RECOVERED", "QUIT"):
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
    """Force live STT streams to reconnect.

    Sarvam: re-applies update_options (its reconnect signal).
    Groq / OpenAI: sets each stream's _reconnect_event to tear down and reopen
    the WebSocket — used by the stall watchdog to recover a wedged pipeline."""
    streams = list(getattr(stt_inst, "_streams", ()))
    if not streams:
        return

    refreshed = 0
    if STT_PROVIDER == "sarvam":
        for stream in streams:
            update_options = getattr(stream, "update_options", None)
            if not callable(update_options):
                continue
            try:
                update_options(language="en-IN", model="saaras:v3", mode="transcribe")
                refreshed += 1
            except Exception as e:
                logger.warning("Could not refresh Sarvam STT stream: %s", e)
    else:
        # Groq/OpenAI WebSocket-based streams: setting _reconnect_event causes
        # the stream's _run loop to drop the current WS and reopen.
        for stream in streams:
            evt = getattr(stream, "_reconnect_event", None)
            if evt is None:
                continue
            try:
                evt.set()
                refreshed += 1
            except Exception as e:
                logger.warning("Could not signal STT stream reconnect: %s", e)

    if refreshed:
        logger.info("Refreshed %d %s STT stream(s)", refreshed, STT_PROVIDER)


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

    # Pre-build the Google TTS grpc client NOW, on the boot path. The livekit
    # google plugin otherwise builds it lazily inside _ensure_client during the
    # first synthesis — which runs on livekit's job thread while audio/STT
    # threads are active, where grpc's aio channel deadlocks starting its poller
    # thread (the same call is ~0.1s here, but hangs forever on the busy job
    # thread). Building it once at boot means the first greeting finds it ready.
    #
    # NOTE: this MUST run directly on the entrypoint's event loop (the same loop
    # the session uses), not via run_in_executor — grpc.aio binds the channel to
    # the running loop, and an executor thread has no loop (the call would fail).
    if TTS_PROVIDER == "google":
        ensure_client = getattr(tts_inst, "_ensure_client", None)
        if callable(ensure_client):
            try:
                _tts_client = ensure_client()
                logger.info("Google TTS client pre-warmed")
                # Force the grpc channel to actually CONNECT now, on the quiet boot
                # loop, with a lightweight unary RPC. Otherwise grpc establishes the
                # HTTP/2 connection lazily during the first greeting — and doing that
                # under the session's concurrent load hangs forever in cygrpc
                # initiate_stream_stream on Windows' ProactorEventLoop (confirmed via
                # asyncio task-stack dump). Connecting here means the greeting reuses
                # an already-open channel.
                try:
                    await asyncio.wait_for(_tts_client.list_voices(), timeout=20)
                    logger.info("Google TTS grpc channel connected (warmup)")
                except Exception as e:
                    logger.warning("Google TTS channel warmup failed: %s", e)
            except Exception as e:
                logger.warning("Google TTS client pre-warm failed: %s", e)

    # Keep a small always-on core tool surface warm, and route heavier domains
    # in only when the current request needs them.
    tool_pool = LocalDomainToolPool(repo_root=_REPO_ROOT)
    core_toolset = tool_pool.get_toolset("core")
    logger.info("Domain tool pool prepared (%s)", _REPO_ROOT / "server.py")

    # Pre-warm the core toolset in the background so the first tool call is instant
    asyncio.create_task(core_toolset.setup())

    session = AgentSession(
        # No AEC warmup grace period — its purpose is to suppress interruptions
        # while AEC learns the speaker profile, but our voice gate already
        # filters self-transcripts and warmup just made input invisible for
        # ~3s after every activation.
        aec_warmup_duration=0.0,
        conn_options=build_session_conn_options(),
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(
                min_delay=_endpointing_delay(),
                max_delay=0.8,
            ),
            # Verbal interruption disabled — only the launcher's PTT keybind
            # interrupts an in-flight turn (via INTERRUPT stdin command below).
            # discard_audio_if_uninterruptible=True DROPS mic input while the
            # agent is speaking. This is the echo guard: console AEC is forced
            # off (it was eating the user's voice) and the per-transcript speaker
            # gate is off, so without this the agent would transcribe its own TTS
            # picked up by the mic and act on it. The mic is gated off during the
            # greeting anyway, so this doesn't clip the first reply.
            interruption=InterruptionOptions(
                enabled=False,
                discard_audio_if_uninterruptible=True,
            ),
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

    # Fatal-close recovery state. `_fatal[0]` is raised by the close handler when
    # an unrecoverable STT/connection error closes the session; `_intentional[0]`
    # is raised before our OWN aclose()/QUIT so the handler ignores those.
    _fatal = [False]
    _intentional = [False]

    _signing_off = False   # guard against interim+final double-fire

    # ---- STT stall watchdog -------------------------------------------------
    # Detects the failure mode where mic is enabled and VAD detects speech, but
    # no transcripts ever arrive (seen after long idle: STT WebSocket goes
    # stale, fails silently, no exception is raised). Recovery is to signal
    # the STT streams to reconnect.
    _mic_enabled = False
    _mic_enabled_at = 0.0       # monotonic ts when mic was last enabled
    _last_speech_started = 0.0  # monotonic ts of last VAD speech-start
    _last_transcript_at = 0.0   # monotonic ts of last user_input_transcribed
    STT_STALL_TIMEOUT = 8.0
    STT_WATCHDOG_INTERVAL = 2.0
    # Cooldown: don't fire watchdog recovery more than once per 15s.
    # Otherwise back-to-back firings tear down connections that just need
    # more time, creating a perpetual reconnect loop with no transcripts.
    STT_RECOVERY_COOLDOWN = 15.0
    _last_recovery_at = 0.0
    # If mic is enabled this long with zero VAD/transcript activity, the
    # audio pipeline itself is wedged (not just STT).
    DEAD_PIPELINE_TIMEOUT = 15.0

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev):
        nonlocal _signing_off, _last_transcript_at
        _last_transcript_at = asyncio.get_event_loop().time()
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

    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        nonlocal _last_speech_started
        if ev.new_state == "speaking" and _mic_enabled:
            _last_speech_started = asyncio.get_event_loop().time()

    async def _stt_watchdog():
        nonlocal _last_speech_started, _mic_enabled_at, _last_recovery_at
        while True:
            try:
                await asyncio.sleep(STT_WATCHDOG_INTERVAL)
                if not _mic_enabled:
                    continue
                now = asyncio.get_event_loop().time()
                # Cooldown: prevent reconnect-storm where back-to-back
                # firings tear down WebSockets faster than Whisper can
                # produce a transcript on them.
                if now - _last_recovery_at < STT_RECOVERY_COOLDOWN:
                    continue
                stall_reason: str | None = None
                # Case 1: VAD detected speech but no transcript followed.
                # 0.5s tolerance: transcripts always lag the speech-start event.
                if _last_speech_started > _last_transcript_at + 0.5:
                    elapsed = now - _last_speech_started
                    if elapsed >= STT_STALL_TIMEOUT:
                        stall_reason = (
                            f"VAD detected speech {elapsed:.1f}s ago but no "
                            f"transcript — STT WebSocket likely stale."
                        )
                # Case 2: mic enabled for a long time with ZERO activity
                # (no VAD speech-start events at all, no transcripts since
                # enable). Audio pipeline itself is wedged — VAD wasn't even
                # told there was sound to look at.
                if stall_reason is None and _mic_enabled_at > 0:
                    enabled_for = now - _mic_enabled_at
                    last_event = max(_last_speech_started, _last_transcript_at)
                    if (enabled_for >= DEAD_PIPELINE_TIMEOUT
                            and last_event <= _mic_enabled_at):
                        stall_reason = (
                            f"mic enabled {enabled_for:.1f}s with zero VAD "
                            f"or transcript events — audio pipeline wedged."
                        )
                if stall_reason is None:
                    continue
                logger.warning("STT watchdog: %s Triggering recovery.", stall_reason)
                try:
                    session.input.set_audio_enabled(False)
                    await asyncio.sleep(0.5)
                    _refresh_stt_streams(stt_inst)
                    session.input.set_audio_enabled(True)
                    # Reset stall windows so we don't re-fire immediately.
                    _last_speech_started = 0.0
                    _mic_enabled_at = asyncio.get_event_loop().time()
                    _last_recovery_at = asyncio.get_event_loop().time()
                    logger.info("STT watchdog: recovery attempt completed")
                except Exception as e:
                    logger.error("STT watchdog: recovery failed: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("STT watchdog loop error: %s", e)

    # The watchdog assumes chunked-request STTs (Groq via OpenAI /transcriptions)
    # where the WebSocket goes silent means it's dead. Deepgram streams
    # continuously and can legitimately go 8+ seconds between FINAL transcripts
    # if the user pauses mid-sentence — refreshing the stream drops in-flight
    # audio and loses the utterance entirely.
    # TEMP (per user request, one session): STT watchdog fully disabled to rule
    # it out of boot problems. Restore the block below to re-enable it (only runs
    # for chunked STTs like Groq; already skipped for deepgram/google anyway).
    #   if STT_PROVIDER not in ("deepgram", "google"):
    #       asyncio.create_task(_stt_watchdog())
    #   else:
    #       logger.info("STT watchdog disabled for streaming provider %r", STT_PROVIDER)
    logger.info("STT watchdog TEMPORARILY DISABLED (user request, this session)")

    # Pre-start the session so VAD + room connection is warm before the
    # first "hey friday". Silence the mic immediately — we don't want
    # transcripts landing in the LLM before the user actually wakes it.
    voice_agent = FridayAgent(stt=stt_inst, llm=llm_inst, tts=tts_inst)
    await session.start(
        agent=voice_agent,
        room=ctx.room,
    )
    try:
        session.input.set_audio_enabled(False)
        _mic_enabled = False
    except Exception as e:
        logger.warning("Failed to pre-disable audio input: %s", e)
    logger.info("Session pre-warmed, audio gated off — awaiting first START")
    print("FRIDAY_READY", flush=True)

    # ---- Stdin dispatcher: handles START/QUIT (queued) and INTERRUPT (inline)
    cmd_queue: asyncio.Queue = asyncio.Queue()
    asyncio.create_task(_stdin_dispatch_loop(session, cmd_queue))

    @session.on("close")
    def _on_session_close(ev):
        # Sync emit callback, runs on the session loop. On a fatal close, signal
        # the launcher and unblock the activation loop so the process exits
        # through its normal cleanup; the launcher then respawns us.
        if not is_fatal_close(ev.reason, intentional=_intentional[0]):
            return
        logger.error("Session closed fatally (reason=%s, error=%s) — exiting for respawn",
                     getattr(ev.reason, "value", ev.reason), ev.error)
        _fatal[0] = True
        print("SESSION_FATAL", flush=True)
        dismissed.set()                      # unblock the ACTIVE `await dismissed.wait()`
        try:
            cmd_queue.put_nowait("FATAL")    # unblock the idle `await cmd_queue.get()`
        except Exception:
            pass

    # ---- Activation loop (first START + every subsequent one use the same path) ----
    while True:
        logger.info("Waiting for START command on stdin…")
        cmd = await cmd_queue.get()
        if cmd not in ("START", "START_RECOVERED"):
            logger.info("Received %r — shutting down", cmd or "EOF")
            break
        recovered = cmd == "START_RECOVERED"

        # Re-activate: clear dismissal, refresh STT, then generate the ready line
        _signing_off = False
        dismissed.clear()
        voice_agent._gate_reject_streak = 0
        voice_agent._gate_fail_open = False
        print("SESSION_STARTED", flush=True)

        try:
            _refresh_stt_streams(stt_inst)
        except Exception as e:
            logger.warning("STT refresh failed before activation: %s", e)

        try:
            await session.generate_reply(
                instructions=(
                    RECOVERY_LINE_INSTRUCTIONS if recovered else _READY_LINE_INSTRUCTIONS
                ),
                tool_choice="none",
            )
            logger.info("%s ready line completed",
                        "Recovery" if recovered else "Re-activation")
        except Exception as e:
            logger.warning("Ready line failed: %s", e)

        # Enable mic after the ready line so the agent doesn't hear itself.
        try:
            session.input.set_audio_enabled(True)
            _mic_enabled = True
            now = asyncio.get_event_loop().time()
            _mic_enabled_at = now
            _last_speech_started = 0.0
            _last_transcript_at = now  # reset stall window on each activation
            logger.info("Audio input enabled — listening for user speech")
            print("SESSION_LISTENING", flush=True)
        except Exception as e:
            logger.warning("Failed to enable audio input: %s", e)


        # Stay active until user dismisses ("that'll be all", etc.) or a fatal close.
        await dismissed.wait()
        if _fatal[0]:
            logger.info("Fatal close — breaking activation loop for clean exit")
            break
        logger.info("Session dismissed — gating mic")
        try:
            session.input.set_audio_enabled(False)
            _mic_enabled = False
        except Exception:
            pass
        print("SESSION_DONE", flush=True)

    # Clean up
    _intentional[0] = True
    try:
        await session.aclose()
    except Exception:
        pass
    try:
        await tool_pool.aclose()
    except Exception:
        pass
    ctx.shutdown("stdin closed" if not _fatal[0] else "fatal session close")


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
