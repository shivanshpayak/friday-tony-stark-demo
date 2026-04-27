# Keybind-Only Interruption — Design

**Date:** 2026-04-26
**Status:** Approved, ready for implementation

## Problem

Verbal interruption (talking over Jarvis to cut him off) sometimes leaves the
session in a stuck state. The desired model is: only an explicit user keybind
interrupts an in-flight turn — voice never does.

## Solution

Disable LiveKit's verbal-interruption path entirely. Overload the existing
push-to-talk hotkey (`ctrl+alt+space`) so it becomes context-aware:

| Launcher state | Press behavior |
|---|---|
| `SLEEPING` | Wake (existing PTT path, unchanged) |
| `ACTIVE` (mid-turn: thinking / tool / speaking) | Cancel the active turn and listen |
| `ACTIVE` (idle between turns) | No-op (LiveKit's `session.interrupt()` is safe to call when no turn is active) |

A single `session.interrupt()` call cancels everything in flight — LLM
generation, queued TTS, and active speech playback. Any in-flight HTTP tool
call completes in the background; its result is discarded.

## Architecture

Turn-state knowledge stays in the agent. The launcher only signals user
intent — it does not need to track speaking-vs-listening transitions.

### Process responsibilities

- **`friday_launcher.py`** — existing `_ptt_press` handler branches on
  `state`. If `SLEEPING`, take the existing wake path. If `ACTIVE`, write
  a new `INTERRUPT` line to the agent's stdin.
- **`agent_friday.py`** — the existing stdin command loop already reads
  `START`. Add an `INTERRUPT` branch that calls `session.interrupt()`. Set
  `allow_interruptions=False` on the `AgentSession` so VAD-based verbal
  interruption is disabled at the SDK level.

### Why not have the launcher track speaking state

That would require new stdout signals (`SESSION_SPEAKING_START` / `_END`)
and a launcher state-machine update, plus inter-process race handling. The
agent already owns turn lifecycle; routing intent to it and letting it
decide is simpler.

## Edge cases

- **Press during slow tool** — `session.interrupt()` cancels the turn;
  in-flight HTTP completes off-screen and result is discarded.
- **Press while STT is mid-transcription of unwanted speech** — interrupt
  clears the active turn; fresh listening begins on the next user utterance.
- **Rapid double-press** — second press is a no-op (no active turn after
  the first interrupt).
- **Press while asleep** — existing PTT wake path runs unchanged.

## Out of scope

- No audible cue on interrupt (silent).
- No new verbal stop phrases. Only the keybind interrupts.
- Dismissal phrases (`"that'll be all"`, `"goodbye jarvis"`) still end the
  session as before — they terminate, not interrupt.
- No keybind for dismissal (kill switch already covers full teardown).

## Files touched

- `friday_launcher.py` — branch the PTT handler (~10 lines added)
- `agent_friday.py` — add `INTERRUPT` stdin command + `allow_interruptions=False` (~15 lines)
