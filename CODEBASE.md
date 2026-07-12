# FRIDAY Codebase Reference

Last rebuilt: 2026-04-19

This is the practical map of the repo. It favors current reality over aspirational architecture.

---

## What FRIDAY is right now

FRIDAY is a Windows voice assistant with:

- an always-on wake-word loop,
- speaker verification,
- one long-lived LiveKit voice session,
- a fullscreen overlay,
- and a local MCP server (`server.py`) that is the single source of truth for tools.

Entry point: `friday_launcher.py`

---

## The one thing to understand first

FRIDAY runs as three cooperating processes:

1. **`friday_launcher.py`** — owns wake word, overlay, hotkeys, and the agent subprocess.
2. **`agent_friday.py`** — one long-lived LiveKit voice session. Consumes tools over a local stdio MCP connection.
3. **`server.py`** — FastMCP server, spawned by the agent over stdio. Registers every tool from `friday/tools/`.

### Where tools live

- `friday/tools/{web,system,apps,utils}.py` — every tool, defined once.
- `friday/tools/__init__.py::register_all_tools` — registers the tool modules with the FastMCP instance.
- `agent_friday.py` — wraps `MCPServerStdio` in an `MCPToolset` and passes it to `AgentSession(tools=[...])`. No `@agents.function_tool` on the agent anymore.

Adding a new tool means editing the relevant module under `friday/tools/` (and adding a new module to `register_all_tools` if you created one). The agent picks it up automatically on next boot because the MCP server re-registers on every spawn.

---

## Latency posture

This repo already has the beginnings of a low-latency design. Preserve them.

### What keeps FRIDAY responsive

- The wake-word loop stays separate from the conversation session.
- The agent subprocess stays alive across dismissals.
- Providers are warmed once on boot instead of recreated every turn.
- Provider builders (STT, LLM, TTS, speaker gate) run in parallel during warm-up.
- The LiveKit session is pre-started before `FRIDAY_READY` so the first "hey jarvis" pays no session-start cost.
- LLM output streams, and TTS begins on the first meaningful chunk.
- `MAX_HISTORY_ITEMS = 8` keeps prompt size small.
- Gemini is configured with `thinking_budget = 0` for lower latency.
- VAD is tuned aggressively to reject noise instead of wasting turns.

### What would make FRIDAY feel slow

- Waiting for a whole multi-step workflow before speaking at all.
- Adding long network-bound tool calls directly into the normal reply path.
- Rebuilding provider clients on every activation.
- Treating research, installs, or delegation as if they were normal conversational turns.
- Putting too much memory or retrieval work into every utterance.

### Practical rule

Fast actions belong in the reply path.
Slow actions should become background tasks with quick acknowledgments.

---

## Runtime map

### `friday_launcher.py`

Main orchestrator.

Responsibilities:
- wake-word loop,
- speaker verification at wake time,
- overlay lifecycle,
- subprocess management for `agent_friday.py`,
- global hotkeys,
- restart and kill behavior,
- top-level logging.

Why it matters:
- This is the real product shell.
- Anything that delays or destabilizes this file hurts the whole experience.

### `agent_friday.py`

The actual voice agent subprocess.

Responsibilities:
- mic selection for the active session,
- one persistent `AgentSession`,
- spawning and wiring the local MCP server (stdio),
- in-session speaker gating,
- dismissal handling (with mic detach to stop STT while asleep),
- streaming LLM and TTS behavior.

Why it matters:
- This is where conversational feel is won or lost.
- New slow tools should not blindly be bolted onto the normal turn path here — add them to `friday/tools/` and let the task layer (future Phase 0.5) handle them.

### `friday_overlay.py`

Visual feedback layer.

Responsibilities:
- waking,
- listening,
- thinking,
- speaking,
- dismissing states.

Why it matters:
- If FRIDAY is doing work, the overlay should reflect it immediately even before speech starts.

### `server.py`

FastMCP server entrypoint. Default transport is **stdio** (spawned by the agent). `--sse` and `--streamable-http` flags are available for external clients later.

Current status:
- live source of truth for tools,
- spawned automatically by the agent subprocess,
- do not run it manually alongside the agent — each agent boot spawns its own instance.

Why it matters:
- every skill lives here or in `friday/tools/`,
- future clients (phone bridge, Telegram, etc.) can attach to the same tool surface over SSE or streamable HTTP.

### `friday/tools/`

Home for all skills.

Current modules:
- `web.py` — `get_world_news`, `search_web`, `open_world_monitor`, `create_document`
- `system.py` — `get_current_time`, `get_system_info`
- `apps.py` — `launch_app`, `close_app`, `rescan_apps`
- `utils.py` — `format_json`, `word_count`
- `media.py` — `play_pause_media`, `next_track`, `previous_track`, `search_spotify` (tracks, playlists, albums), `set_volume` (master + per-app via pycaw)
- `files.py` — `list_files`, `read_file`, `search_files` (bounded by `FRIDAY_FILE_ROOTS`)
- `audio.py` — `recognize_song_humming` (Gemini multimodal, records mic then identifies)
- `messaging.py` — `draft_message` (WhatsApp URI / Discord clipboard+open)
- `google_suite.py` — `list_upcoming_events`, `list_recent_emails` (Google OAuth, read-only)
- `memory.py` — `remember`, `forget`, `list_memories` (persistent cross-session memory in `runtime/memory.json`)

Rule:
- every tool lives here. No shadow definitions on `FridayAgent`.
- add new modules to `friday/tools/__init__.py::register_all_tools`.

### `friday/tasking/`

Phase 0.5 task orchestration layer.

- `models.py` — `TaskRecord` and `TaskStep` Pydantic models.
- `store.py` — JSON persistence in `runtime/tasks/active/`.
- `router.py` — `classify_request()` keyword heuristic (fast vs task).
- `planner.py` — `plan_steps()` calls LLM in planner mode to build a step list.
- `service.py` — `start_task()` entry point; launches `standalone_executor.py` in a CMD window; file-watcher loop polls task JSONs every 3s and fires the completion callback when the standalone executor finishes.
- `executor.py` — in-process executor (unused for the CMD path, but holds `_CALLBACK` and `set_completion_callback`).
- `standalone_executor.py` — runs in the spawned CMD window; boots its own MCP connection and Gemini LLM, executes up to 15 tool steps, writes final_summary to task JSON.

---

## Current process model

1. Start `friday_launcher.py`.
2. Warm the agent subprocess.
3. Wait in the wake-word loop.
4. Verify the speaker.
5. Signal the already-running agent session.
6. Stream the reply.
7. Return to sleep without destroying the whole process tree.

This design is good. Keep it.

---

## Where to add new capabilities

### If the feature is fast and local

Examples:
- app launch,
- room volume change,
- file listing,
- current window info.

Best home:
- `friday/tools/` long-term,
- and temporarily `agent_friday.py` too if MCP migration is still incomplete.

### If the feature is slow or multi-step

Examples:
- research,
- web-heavy summaries,
- Gmail triage,
- Claude delegation,
- shell installs,
- batch file cleanup.

Best shape:
- start quickly,
- return a short acknowledgment,
- run as a tracked task,
- speak again on completion or when user asks.

### If the feature is external-device control

Examples:
- Google Home,
- Hue,
- Home Assistant,
- cameras,
- thermostats.

Best shape:
- use a bridge,
- keep auth and vendor-specific weirdness outside the hot conversation path,
- expose a narrow tool interface to FRIDAY.

---

## Google Home / Nest note

For this repo, Google Home is a bridge problem, not a direct Python SDK problem.

### Good fit

- `friday/tools/home_assistant.py`
- Home Assistant discovers Cast / Nest devices
- FRIDAY calls simple home-control tools

### Hard no for now

- using Nest speakers as FRIDAY microphones,
- custom wake-word routing through Google Home devices,
- deep Google Home integration directly inside this Windows repo without a companion app.

### If deeper Google Home support is needed later

Build a small Android or iOS companion app that owns Google Home auth and exposes a narrow local API back to FRIDAY.

---

## Voice and TTS

- **TTS provider**: Google Gemini TTS (`gemini-2.5-flash-tts`) via `lk_google.TTS`.
- **Voice**: Charon — selected for JARVIS-like calm, professional delivery.
- **Auth**: Requires a Google Cloud service account with Vertex AI User role. Set `GOOGLE_APPLICATION_CREDENTIALS` in `.env`.
- **Tuning**: `speaking_rate=1.3`, `volume_gain_db=-4.0`, style prompt emphasizes quick natural flow without over-emphasizing syllables.
- **System prompt style**: "sir" only (never the user's name), no enthusiasm, no filler, concise and direct.

## Persistent memory

- `friday/tools/memory.py` stores key-value facts in `runtime/memory.json`.
- Memories are injected into the system prompt at agent init via `get_memories_prompt()`.
- Tools: `remember(key, value)`, `forget(key)`, `list_memories()`.
- Use case: user preferences like "when I say play lofi, play the playlist" persist across sessions.

---

## Important implementation constraints

1. One subprocess, one long-lived session.
2. Two speaker gates are intentional: strict at wake, looser in-session.
3. `SESSION_MIC` must be applied before LiveKit imports.
4. The tool leak scrubber is only needed for llama-family models.
5. Tools live only in `friday/tools/` and are consumed through MCP stdio. Don't add `@agents.function_tool` to `FridayAgent`.
6. The LiveKit session is pre-started during boot (before `FRIDAY_READY`) with `session.input.set_audio_enabled(False)` so VAD load + room connection cost is paid up-front. Audio is enabled on the first `START` and every subsequent one. On dismissal the agent disables audio again — without this the STT pipeline self-reactivates ~1–2 min later. Greetings fire from the activation loop in `entrypoint()`, not from `on_enter`, so the pre-started session stays silent until the user actually wakes it.
7. EcoQoS handling matters for smooth overlay and wake responsiveness.
8. Stale tests exist and should not be treated as authoritative.

---

## Files that matter most

### Core runtime

- `friday_launcher.py`
- `agent_friday.py`
- `friday_overlay.py`
- `friday/config.py`
- `friday/providers.py`
- `friday/speaker_gate.py`

### Tooling surface

- `server.py`
- `friday/tools/__init__.py`
- `friday/tools/system.py`
- `friday/tools/web.py`
- `friday/tools/apps.py`
- `friday/tools/media.py`
- `friday/tools/files.py`
- `friday/tools/google_suite.py`

### Task orchestration

- `friday/tasking/service.py` — `start_task()` entry point
- `friday/tasking/standalone_executor.py` — the visible CMD sub-routine
- `friday/tasking/store.py` — task JSON persistence
- `friday/tasking/router.py` — fast vs task classification

### Operational support

- `list_audio_devices.py`
- `enroll_voice.py`
- `start_friday.bat`
- `start_friday.vbs`

---

## Environment variables

Loaded from `.env` in the repo root.

| Var | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Gemini provider access |
| `OPENAI_API_KEY` | OpenAI provider access |
| `GROQ_API_KEY` | Groq provider access |
| `SARVAM_API_KEY` | Sarvam STT or TTS |
| `DEEPGRAM_API_KEY` | Deepgram TTS (unused, switched to Google) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service account JSON for Google Gemini TTS (Vertex AI) |
| `LIVEKIT_URL` | LiveKit config |
| `LIVEKIT_API_KEY` | LiveKit config |
| `LIVEKIT_API_SECRET` | LiveKit config |
| `WAKE_MIC` | wake-word input device selector |
| `SESSION_MIC` | active conversation mic selector |

---

## How to run it

```bash
uv run friday_start
```

Useful variants:

```bash
uv run friday_voice
wscript.exe start_friday.vbs
.venv\Scripts\python.exe list_audio_devices.py
.venv\Scripts\python.exe enroll_voice.py
```

Logs:
- `logs/friday.log`

---

## What to optimize next

If the goal is "Jarvis, but still fast," the next engineering wins are:

1. ~~Finish the MCP migration without regressing latency.~~ Done (2026-04-15).
2. ~~Add a minimal background task layer for slow tools (Phase 0.5).~~ Core shipped (2026-04-17).
3. ~~Add fast local tools before network-heavy skills.~~ Media, files, apps all live in `friday/tools/`.
4. ~~Wire the task completion callback.~~ Done (2026-04-19). File-watcher in `service.py` polls task JSONs every 3s and fires `_on_task_finished` when a task completes.
5. Put home-device control behind a local bridge (Phase 3 — Home Assistant).
6. Phase 6b headless `ask_claude` — the completion callback is now wired, so this is unblocked.
7. Keep spoken replies short unless the user explicitly asks for a long task result.

---

## Debugging history & hard-won fixes (June–July 2026)

A long debugging saga fixed FRIDAY going deaf/mute on Windows. Recording it so future
work doesn't re-derive it — or worse, rip out a fix that looks unnecessary. The
non-obvious workarounds all live at the top of `agent_friday.py` with escape hatches.
See `ARCHITECTURE.md` §6 for the diagram version.

### What broke → root cause → fix (DO NOT UNDO without reading why)

| Symptom | Root cause | Fix (where) | Escape hatch |
|---------|-----------|-------------|--------------|
| Greeting/LLM connection **hangs forever** on activation | Windows `ProactorEventLoop` wedges when a *losing* IPv4/IPv6 "happy-eyeballs" TCP connect is **cancelled** (`_OverlappedFuture cancelled`, task stuck "cancelling"). anyio/httpx never completes the connection. | `_force_ipv4_resolution` + `_patch_anyio_happy_eyeballs` (sequential connect) — top of `agent_friday.py` | `FRIDAY_DISABLE_NET_PATCHES=1` |
| First **Google TTS** synthesis hangs (grpc `initiate_stream_stream`) | grpc opens the HTTP/2 channel lazily on first call, under session load, and hangs | Pre-warm `list_voices()` at boot to connect the channel early | — |
| **"Not listening"** — audio flows but STT returns nothing, no transcripts | livekit's console `AudioProcessingModule` (AEC + noise-suppression + AGC) stripped the mic to the noise floor **before the STT** (raw mic peak ~7800, frames reaching STT ~26) | `_force_console_apm_off` (passthrough APM) | `FRIDAY_KEEP_APM=1` |
| Agent would transcribe **its own TTS** (once APM + gate off) | no echo cancellation left | echo guard: `discard_audio_if_uninterruptible=True` in the AgentSession | — |
| Speaker gate **rejected the owner's own voice** → PTT-spam needed | enrolled profile vs. current acoustics (user relocated); per-transcript verification too strict for a single-user setup | `SESSION_SPEAKER_GATE_ENABLED = False` (config) | flip flag back to `True` |
| Agent **fails to boot / no `FRIDAY_READY`** | a debug monkeypatch of `AudioProcessingModule.process_stream` that `print()`ed from the sounddevice **callback thread** jammed startup | removed it; patch the **constructor**, never the `process_stream` method | — |
| Boot crash: `hey_jarvis_v0.1.onnx` missing / slow first boot | `uv run` rebuilt the venv (out of sync with lock) → wiped downloaded openWakeWord models + un-compiled `.pyc` | re-run `openwakeword.utils.download_models()`; second boot is fast | — |
| STT session dies → FRIDAY goes deaf, no respawn | agent didn't exit on fatal close, so `agent.alive` stayed True | recovery feature: exit on `CloseReason.ERROR` → launcher respawns (`friday/recovery.py`) | — |
| MCP tool `read_screen` times out (30s) | `server.py` is a **separate process without the net patches**; heavy Gemini-vision upload from a slow link exceeds the MCP cap | (open) shrink screenshot + fit timeout; consider applying net patches to `server.py` | — |

### Debugging lessons — so future chats don't repeat the flailing

- **Audio "not listening": measure the peak amplitude of frames *at `stt_node`* FIRST.** Do not assume it's the STT provider, network, sample rate, or the user's location. We burned days on those; a one-line peak probe (raw mic vs. frames-into-STT) localized it to the APM instantly.
- **Connection hangs on Windows are usually the ProactorEventLoop connect-wedge, not the network.** Tell-tale: everything works **standalone**, only the full in-agent runtime hangs. Diagnose with `uvx py-spy dump --pid <agent_pid>` (thread stacks) and an in-process `asyncio.all_tasks()` + `task.print_stack()` dump (async await points). The thread stack just shows the idle event loop; the *task* stack shows the real wedge.
- **Prove providers work standalone** (script that hits Gemini/Deepgram/TTS directly) to rule out network/location — but remember the bug may only surface under the agent's concurrency / mic contention. Reproduce that (e.g. hold the mic open during boot).
- **Never `print()` from a sounddevice/PortAudio callback thread** into a piped stdout — it can deadlock boot. Instrument via the logger or a file, and patch constructors, not hot-path audio methods.
- **`server.py` runs as its own process** and does *not* inherit `agent_friday.py`'s module-level patches. Network-calling tools can hang there independently.
- **`uv run` may silently rebuild the venv.** Expect wiped on-demand model downloads and a slow first boot afterward.

### Guardrails put in place to prevent repeats

- `ARCHITECTURE.md` — diagram-based architecture; **§6 is the gotchas table**. CLAUDE.md now instructs every session to keep it updated as features change.
- Every workaround has a **clear code comment explaining the failure it prevents** and an **env-var escape hatch** so it's reversible/testable rather than mysterious.
- This CODEBASE.md section — the narrative record above.

---

## Related docs

- `ARCHITECTURE.md`: diagram-based architecture, audio pipeline, feature map, platform gotchas
- `CAPABILITY_PLAN.md`: product direction and build order
- `OVERLAY_REWRITE.md`: overlay design details
