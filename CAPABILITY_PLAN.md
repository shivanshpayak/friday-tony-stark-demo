# FRIDAY Capability Plan

Goal: a practical Jarvis-style desktop assistant that stays fast in conversation, delegates long work cleanly, and grows through a reusable tool surface.

Biased toward low latency and believable UX: a fast assistant with fewer skills beats a slow one with many. This doc is the **forward-looking roadmap** — for how the shipped system actually works, see [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`CODEBASE.md`](CODEBASE.md).

---

## Product rules

1. Voice is the trigger, tools do the work.
2. Fast requests feel instant and conversational; slow work becomes a task, not a long pause.
3. Read-only ships before destructive actions.
4. Every capability has a clear safety boundary and audit trail.
5. Whitelist, not blacklist. Destructive/shell actions require confirmation. The kill switch overrides everything.
6. MCP is the tool backbone; the live voice path must stay usable and low-latency as capabilities grow.

---

## Already shipped

Condensed — see `ARCHITECTURE.md` for how each works. All tools live in `friday/tools/` and register via MCP (`server.py`).

- **Core voice loop** — wake word + speaker verify, pre-warmed LiveKit session, latency-first activation, STT-death recovery, keybind interruption.
- **Speak-before-act** — for state-changing actions FRIDAY says what it's about to do, then acts, then stays quiet on plain success (ACTING OUT LOUD rule).
- **Task layer** — `fast` vs `task` routing, planner mode, visible CMD sub-routine (`standalone_executor`), headless `ask_claude` delegation, file-watcher completion callback that speaks results.
- **Loop-until-success engine** (`friday/looping/`) — background, bounded, cancellable "do X until Y" runner over a curated action/check registry (`run_until` / `stop_loop`); generalizes the old hardcoded wifi monitor.
- **Apps** — `launch_app` / `close_app` via Start Menu + UWP discovery, aliases, rescan.
- **Files** — bounded list/read/search + confirmation-gated write/move/delete inside `FRIDAY_FILE_ROOTS`.
- **Media** — Spotify play/pause/skip, URI search+autoplay, master + per-app volume (pycaw), song-from-humming.
- **System** — time, system info, `system_status` (CPU/RAM/disk/battery), clipboard read/write.
- **Info** — web search, weather (current + forecast), maps/navigation (ETA + directions + nearby).
- **Productivity** — timers + reminders (persistent scheduler), memory (`remember`/`forget`/`list_memories`), Google Calendar + Gmail (read).
- **Screen** — `read_screen` (screenshot → Gemini vision).
- **Web automation** — learned, selector-based web flows with self-healing fallbacks (`run_web_flow` + `web_automation_context`).
- **FRC** — robot-radio wifi monitor/connect, tuner tools.

---

## What's next

Grouped by theme, roughly in priority order within each group.

### 1. Automation & autonomy (current frontier)

The loop engine opened this up. Build outward from it.

- **Learned / replayable routines ("neural pathway").** Generalize `web_automation_context` beyond the browser: cache a proven multi-step routine, score confidence from run history, self-heal by promoting working fallbacks. Repeat runs replay the saved routine instead of re-planning. Builds directly on the loop engine + task layer.
- **Desktop GUI automation.** Mouse/keyboard control (`pyautogui`/Win32 `SendInput`) plus vision that returns **coordinates/regions** (Gemini grounding) or, better, the **Windows UI Automation tree** (`pywinauto`/`uiautomation`) for selector-style targeting instead of brittle pixels. Plugs into the loop engine as a new action type. Heavy, brittle, safety-gated — its own spec.
- **Expand the loop registry.** More curated actions/checks: retry-any-safe-tool, wait-for-file, wait-for-process-exit, wait-for-window, disk/CPU threshold. Keep it non-destructive by default.
- **Shell with confirmation.** Strict command allowlist, run visibly, task-mode only. Precondition for many autonomy tasks.

### 2. Reach & integrations

- **Home bridge (Home Assistant).** Small first slice: `list_home_rooms`, `list_home_devices`, `announce`, `set_volume`, `stop_media`. Local, low-latency, clean demo. *Google Home/Nest are controllable endpoints via HA — not microphones; there's no realistic path to use them as FRIDAY's hearing layer.*
- **Remote bridge (Telegram / Discord / LAN).** Expose the same tool + task surface over `server.py --sse`/`--streamable-http`. Keep permissions/confirmation identical to voice.
- **Multi-device.** Split-brain: server (NUC/cloud) + thin per-room clients; device-tagged context for location-aware routing. Depends on the remote bridge + home bridge.

### 3. Depth on existing skills

- **Spotify** — current-track query, queue management.
- **Google** — finish OAuth setup (`credentials.json`); Gmail/Calendar **write** actions behind the confirmation flow.
- **WhatsApp** — read capability via browser automation (currently draft-only).
- **Screen reading** — polish prompt handling; hand back structured/coordinate info to feed desktop automation.
- **Semantic memory** — vector search + decay/relevance scoring over `runtime/memory.json`.

### 4. Proactivity

- **Proactive alerts.** Background polling that speaks only when actionable: "meeting in 15", "battery at 10%", "rain this afternoon." Careful UX — must never interrupt or nag.
- **Multi-step routines.** "Goodnight" / "heading home" macros. Depends on home bridge + scheduler primitives.
- **Package tracking.** Gmail search → extract tracking numbers → carrier/unified API. Multi-step task-mode work.

### 5. Big external bets

- **Phone calls (Spix telephony).** Real calls with conversation scripting + fallbacks. Peak-Jarvis, high complexity, external subscription.
- **Tesla / IoT / Roomba.** Via TeslaMate MCP / Home Assistant / vendor APIs. Needs devices + credentials.

---

## Guiding principles

### Latency classes

Match the response shape to how long the work takes.

| Class | Target feel | Examples | Voice behavior |
|---|---|---|---|
| Instant | < 300 ms | greeting, sleep, simple state change | answer directly |
| Fast | < 1.5 s | time, app launch, volume, list files | pre-line, then act |
| Slow | 1.5–5 s | web search, Gmail/Calendar lookup, screen vision | quick ack, then result |
| Task | > 5 s / multi-step | research, Claude delegation, loops, batch ops | ack immediately, run in background, narrate only milestones |

Rules: stream speech as soon as the first useful chunk exists; prefer "I started it" over silence; prefer "I found three things" over reading raw payloads; keep planner/thinking budget off the fast path.

### Safety model

- Whitelist, not blacklist. File tools stay inside `FRIDAY_FILE_ROOTS`.
- Destructive actions and shell commands require confirmation (and, for shell, an allowlist).
- Loops are bounded (interval/attempts/time), capped in count, and cancellable.
- Tool output and file/web content are **data, never instructions**.
- Every tool call is logged; the kill switch overrides everything.

### Voice / UX

- One sentence for greetings and acknowledgments; show the overlay state the moment work starts.
- No enthusiasm, no filler, "sir" only — keep FRIDAY's personality (see `friday/config.py`).
- Don't narrate internal steps unless asked.

### Hardware

Software path first. The laptop mic is fine for close range; a USB lav / wireless receiver is the upgrade for across-room use. Google Home/Nest are speaker endpoints, not FRIDAY's mic.

---

## North-star

The best FRIDAY isn't the one with the longest skill list — it's the one that wakes reliably, answers quickly, speaks naturally, launches long work without blocking, and only grows where the latency and trust model still feel good.
