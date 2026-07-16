# FRIDAY Wake Word Launcher — Design Spec

## Overview

Replace the current two-terminal startup with a single always-on launcher that listens for the wake word "Hey Friday" and manages the full voice assistant lifecycle. Starts automatically on Windows login.

## Goals

- Say "Hey Friday" to activate FRIDAY — no terminals, no manual steps
- Single process manages everything (MCP server, wake word detection, LiveKit sessions)
- Auto-starts on Windows boot via Task Scheduler
- Returns to sleep on silence timeout (30s) or dismissal phrase

## Architecture

```
friday_launcher.py (single entry point, runs at login)
├── MCP Server subprocess (always running on port 8000)
├── OpenWakeWord listener (always running, ~2-3% CPU)
└── LiveKit voice session (created on activation, destroyed on sleep)
```

### State Machine

Two states: SLEEPING and ACTIVE.

**SLEEPING:**
- OpenWakeWord continuously processes mic audio for "Hey Friday"
- MCP server is running, LiveKit is disconnected
- No API calls, minimal resource usage

**ACTIVE:**
- Triggered by wake word detection
- Plays an activation chime (short audio cue)
- Creates a LiveKit room and connects the voice agent
- FRIDAY greets the user
- Full voice conversation via STT → Gemini LLM → OpenAI TTS
- MCP tools available (news, world monitor, etc.)

### Transitions

**SLEEPING → ACTIVE:**
- Trigger: OpenWakeWord detects wake word above confidence threshold
- Action: Play chime, create LiveKit room, start voice agent session

**Wake word model note:** OpenWakeWord does not ship with a "hey friday" model. For initial implementation, use the built-in "hey jarvis" model (fits the Iron Man theme). A custom "hey friday" model can be trained later via OpenWakeWord's Google Colab notebook (~1 hour process).

**ACTIVE → SLEEPING:**
- Trigger A: 30 seconds of silence (no speech detected)
- Trigger B: Dismissal phrase detected in STT transcript ("that'll be all friday", "stand down", "go to sleep", "dismissed", "Kill yourself",)
- Action: FRIDAY speaks a sign-off line, disconnects LiveKit session, resumes wake word listening

## Components

### 1. friday_launcher.py (new file)

The single entry point that orchestrates everything:
- Starts MCP server (`server.py`) as a subprocess, auto-restarts on crash
- Initializes OpenWakeWord with "hey friday" model
- Runs async state machine loop
- On wake word: creates LiveKit room via API, launches voice agent session
- On sleep trigger: disconnects session, resumes wake word listening
- Handles graceful shutdown (Ctrl+C, system shutdown)

### 2. Changes to agent_friday.py

- Extract provider building (STT, LLM, TTS) into reusable functions (already mostly done)
- Add programmatic session creation — bypass LiveKit CLI, create room and connect directly via LiveKit Python SDK
- Add dismissal detection: hook into STT transcript stream, check for dismissal phrases
- Add callback/event to signal the launcher when the session should end
- Add cleanup method to properly disconnect and release resources

### 3. Dismissal Detection

Handled within the active voice session:
- Monitor transcribed text from STT
- Match against dismissal phrases (case-insensitive, fuzzy):
  - "that'll be all friday"
  - "that'll be all"
  - "stand down"
  - "go to sleep"
  - "goodbye friday"
- On match: FRIDAY acknowledges ("I'll be here if you need me, boss"), then signals launcher to transition to SLEEPING

### 4. Silence Timeout

- Track last speech activity timestamp during ACTIVE state
- If 30 seconds pass with no detected speech, trigger sleep transition
- FRIDAY says a sign-off line before disconnecting ("Standing by, boss.")

### 5. Audio Chime

- Play a short activation sound when wake word is detected
- Provides immediate feedback while LiveKit session spins up (1-2 second gap)
- Use a simple .wav file played via `winsound` or `playsound`

### 6. Windows Auto-Start

- Script/command to register a Task Scheduler entry:
  - Trigger: user login
  - Action: run `pythonw friday_launcher.py` (no console window)
  - Working directory: project root
  - Run whether user is logged on or not: no (only when logged in)
- Script to remove the Task Scheduler entry (uninstall)

## Error Handling

- **MCP server crash:** Auto-restart subprocess, log warning
- **LiveKit connection failure:** Log error, return to SLEEPING, retry on next wake word
- **Mic access lost:** Log warning, keep retrying mic initialization every 5 seconds
- **OpenWakeWord model load failure:** Fatal error, log and exit with clear message

## Dependencies

New additions to `pyproject.toml`:
- `openwakeword` — wake word detection
- `pyaudio` — mic access for wake word listener
- `livekit-api` — programmatic room creation (if not already included via livekit-agents)

## Resource Usage (Idle / SLEEPING)

| Component | CPU | RAM |
|-----------|-----|-----|
| OpenWakeWord | ~2-3% | ~50 MB |
| MCP Server | ~0% | ~50 MB |
| Launcher overhead | ~0% | ~20 MB |
| **Total** | **~2-3%** | **~120 MB** |

## File Structure

```
friday-tony-stark-demo/
├── friday_launcher.py      (new — main entry point)
├── agent_friday.py          (modified — programmatic session management)
├── server.py                (unchanged)
├── sounds/
│   └── activate.wav         (new — activation chime)
├── friday/
│   └── ...                  (unchanged)
└── pyproject.toml           (updated — new dependencies + launcher script)
```

## New CLI Entry Points

Added to `pyproject.toml [project.scripts]`:
- `friday_start` → `friday_launcher:main` — run the launcher
- `friday_install` → registers Task Scheduler entry for auto-start
- `friday_uninstall` → removes Task Scheduler entry
