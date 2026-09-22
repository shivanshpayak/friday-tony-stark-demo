# Fast-Path Voice Commands — Design

**Date:** 2026-08-01
**Status:** Approved, ready for implementation plan

## Problem

Simple state-change commands ("mute", "play", "next") take 5–7 seconds end to
end, of which the actual work is ~50 ms. The cost is entirely in the activation
and reasoning path:

| Step | Where | Cost |
|------|-------|------|
| Wake-word detection | [`WakeWordListener.listen_once`](../../../friday_launcher.py) | ~0.3 s |
| Speaker verification (Resemblyzer) | [`SpeakerVerifier.verify`](../../../friday_launcher.py) | ~0.2 s |
| Ready-line greeting — Gemini LLM + Gemini TTS | [`agent_friday.py` activation loop](../../../agent_friday.py) | **~2–3 s** |
| Deepgram STT + endpointing | `stt_node` | ~0.5 s |
| Gemini tool selection | [`FridayAgent.llm_node`](../../../agent_friday.py) | **~1–2 s** |
| MCP round trip → `keyboard.send` | `server.py` → `friday/tools/media.py` | ~0.05 s |
| Gemini TTS confirmation | — | ~1 s |

Two separate costs dominate: the **activation greeting** and the **LLM tool
selection**. Removing only the LLM hop (an in-session interceptor) would leave
the ~3 s greeting in place, so it does not solve the stated problem.

## Goals

- A fixed set of state-change commands executes in well under a second, spoken
  as one breath with the wake word: "Hey Jarvis, mute".
- Adding a command is a one-line change to a table.
- The normal conversational path pays as close to zero added latency as
  practical, and is byte-for-byte unchanged in behavior.
- Every failure mode degrades to today's behavior. The fast path is never
  load-bearing.

## Non-goals

- **Commands with arguments.** No "volume 50", no "open chrome". Fixed phrases
  only, so the recognizer can use a closed grammar. Anything with a variable
  falls through to the normal Gemini path.
- **An in-session fast path.** Once a session is open, turns are handled exactly
  as they are today. `agent_friday.py`'s `llm_node` is not touched.
- **Natural-language flexibility.** "Mute it", "mute the sound", "could you mute"
  are not fast commands unless explicitly added as phrases. Misfires are worse
  than misses here (see Fall-through cost).

## Decisions

Recorded from the design conversation, with the reasoning that settled each:

1. **Launcher-level, session never starts.** The fast path runs entirely inside
   `friday_launcher.py`. The agent subprocess, LiveKit session, Gemini, MCP, and
   TTS are all bypassed. This is the only option that removes the greeting cost.
2. **Fixed phrases only.** A closed grammar gives near-zero misrecognition and a
   clean, unambiguous reject signal.
3. **Vosk with closed-grammar decoding.** `vosk-model-small-en-us-0.15` (~40 MB,
   offline, pure C++, no torch). `KaldiRecognizer` accepts an explicit phrase
   list and can then only emit a grammar phrase or the literal `[unk]` token.
   ~40 ms for a ~1 s clip.
   - Rejected: **openwakeword model per command** — zero latency and zero new
     deps, but needs a ~30-minute training run per command and is weak on
     one-syllable targets like "mute".
   - Rejected: **faster-whisper tiny.en** — ~150–300 ms, needs ctranslate2,
     hallucinates on short clips (this codebase already carries
     `_is_whisper_hallucination` for that reason), and gives no clean reject
     signal.
   - Rejected: **Deepgram one-shot REST** — no new deps, but a ~250–450 ms
     network round trip, and it makes muting the local speakers depend on the
     internet.
4. **Ack is a short tick, no speech.** Sub-100 ms WAV, distinct from the wake
   chime. For mute/play/next the effect is self-evident.
5. **Streaming decode + `PREPARE`/`GREET` split** to minimize the cost imposed
   on the normal path (see Latency).

## Architecture

```
wake word fires (existing)
        │
        ├─► send PREPARE to agent ──────────────────┐ agent refreshes STT
        │                                            │ streams, stays silent
        ├─► speaker verify (on ring-buffer snapshot) │ concurrent
        │                                            │
        └─► TailRecognizer.capture_and_match(stream) ┘
               webrtcvad endpoints the tail
               Vosk streaming closed-grammar decode
                        │
              ┌─────────┴──────────────┐
         matched command          [unk] / no speech / speaker mismatch
              │                        │
        tick + run action        send GREET → existing path
        stay SLEEPING            (greeting → mic on → Gemini)
```

Ordering note: speaker verification gates the fast path exactly as it gates
normal activation. A voice that fails verification runs no action and starts no
session.

### `friday/fastpath/registry.py`

Single source of truth for the command table.

```python
@dataclass(frozen=True)
class FastCommand:
    name: str                      # stable id, used in logs
    phrases: tuple[str, ...]       # lowercase grammar entries
    action: Callable[[], None]     # plain sync callable, no args
```

- `COMMANDS: tuple[FastCommand, ...]` — the table.
- `grammar_phrases() -> list[str]` — flat, de-duplicated list of every phrase
  plus the literal `"[unk]"`, handed to `KaldiRecognizer`.
- `lookup(text: str) -> FastCommand | None` — normalizes (lowercase, strip,
  collapse internal whitespace, strip trailing punctuation) then exact-matches
  against the phrase index. Returns `None` for `[unk]`, empty, or unknown.

Adding a command is one entry in `COMMANDS`.

### `friday/fastpath/actions.py`

Plain synchronous callables. No `mcp`/`fastmcp` imports — the launcher must not
pull FastMCP into its process.

Starter set:

| Command name | Phrases | Action |
|---|---|---|
| `mute` | "mute" | pycaw `EndpointVolume.SetMute(1, None)` |
| `unmute` | "unmute" | pycaw `EndpointVolume.SetMute(0, None)` |
| `play_pause` | "play", "pause" | `keyboard.send("play/pause media")` |
| `next_track` | "next", "skip", "next track" | `keyboard.send("next track")` |
| `prev_track` | "back", "previous", "previous track" | `keyboard.send("previous track")` |
| `louder` | "louder", "volume up" | master volume +10 pts |
| `quieter` | "quieter", "volume down" | master volume −10 pts |
| `stop` | "stop" | `keyboard.send("stop media")` |

Explicit `SetMute(1)`/`SetMute(0)` rather than the mute *toggle* media key, so
"mute" and "unmute" are idempotent and mean what they say.

### Refactor: shared action layer

[`friday/tools/media.py`](../../../friday/tools/media.py) defines
`play_pause_media`, `next_track`, and `previous_track` *inside* `register(mcp)`,
so their bodies cannot be reused without importing FastMCP. Lift each body to a
module-level plain function (`_set_master_volume` and `_get_master_volume`
already are module-level and are reused as-is). The MCP tool wrappers then call
the same module-level function that `fastpath/actions.py` calls.

This is a targeted change confined to functions the fast path needs. No other
tool module is touched.

### `friday/fastpath/recognizer.py`

`TailRecognizer` — model lifecycle, capture, endpointing, decode.

- Constructed at launcher boot; loads the Vosk model on a background thread so
  the first use is warm. `available` property reflects load success.
- `capture_and_match(stream) -> FastCommand | None`, run in an executor.

Capture and endpointing, reading the **already-open** pyaudio stream in
`AUDIO_CHUNK` (1280-sample / 80 ms) reads, sub-framed into 320-sample / 20 ms
frames for `webrtcvad.Vad(2)`:

1. **Lead window** — wait up to `TAIL_LEAD_TIMEOUT` (300 ms) for speech onset.
   No onset → return `None` immediately. This is the bare-"Hey Jarvis" case.
2. **Speech** — accumulate frames, feeding them to `KaldiRecognizer` as they
   arrive (streaming, not batch).
3. ~~**Early bail** on the streaming partial.~~ **Removed** — the spike proved
   Vosk emits no partial for the first ~880 ms, so there is nothing to bail on
   before the cap fires. See "Spike result" below.
4. **End** — stop on `TAIL_TRAILING_SILENCE` (300 ms) of contiguous silence, or
   on the `TAIL_MAX_SPEECH` (1.0 s) hard cap, whichever comes first. Cap hit →
   return `None` without decoding; no fast command is that long.
5. **Decode** — `FinalResult()`, then `registry.lookup()`.

All five constants live in `friday/config.py` so they are tunable without
touching logic.

### `friday/fastpath/model.py`

`ensure_model() -> Path | None`. Checks for
`models/vosk-model-small-en-us-0.15/`; if absent, downloads and unzips it once,
logging progress. Any failure (no network, bad archive, missing dir) logs a
warning and returns `None`, which disables the fast path for that run.

### `PREPARE` / `GREET` protocol split

Today `START` does STT stream refresh and the greeting back to back in
`agent_friday.py`'s activation loop. Split at that boundary:

- **`PREPARE`** — clear dismissal flags, reset the speaker-gate counters, print
  `SESSION_STARTED`, run `_refresh_stt_streams`. Silent; no LLM, no TTS, no mic.
- **`GREET`** — fire the ready line, then enable the mic and print
  `SESSION_LISTENING`.
- **`ABORT`** — sent when a fast command matched, or when speaker verification
  failed. Undoes `PREPARE` (re-set the dismissed flag, leave the mic disabled)
  and returns the agent to the outer wait. No `SESSION_DONE` is printed, because
  no session began.

Agent-side shape: the activation loop's single `await cmd_queue.get()` becomes
two stages. Stage one waits for `PREPARE`/`START`/`START_RECOVERED`/`QUIT` as
today. After running the prep work, `PREPARE` enters a second, bounded wait for
`GREET` or `ABORT` — with a timeout (`PREPARE_WAIT_TIMEOUT`, 5 s) that falls
back to `ABORT` so a launcher crash mid-handshake cannot wedge the agent in a
half-activated state. `START`/`START_RECOVERED` skip the second wait entirely
and proceed straight to the greeting, so the PTT path and the fatal-recovery
path in `friday_launcher.py` need no changes.

`_stdin_dispatch_loop` routes `PREPARE`, `GREET`, and `ABORT` onto the existing
`cmd_queue` alongside `START`; `INTERRUPT` keeps its current inline handling.

The launcher sends `PREPARE` the instant the wake word fires, so the agent's
reconnect work overlaps with tail capture instead of following it.

### Launcher wiring

In `launcher_loop`'s `State.SLEEPING` branch, after wake-word detection: send
`PREPARE`, then run speaker verification and `capture_and_match` concurrently
(verification uses a snapshot of the ring buffer taken *before* capture starts,
so there is no read race on `_buffer`).

- Verification fails → send `ABORT`, `wakeword.reset()`, stay `SLEEPING`.
  Existing mismatch behavior, plus the abort.
- Command matched → play the tick, run the action in an executor, send `ABORT`,
  `wakeword.reset()`, stay `SLEEPING`.
- Otherwise → send `GREET`, `state = State.ACTIVE`. The `ACTIVE` branch skips
  its own `send_start()` because `PREPARE`/`GREET` already covered it — reusing
  the `recovery_started` flag pattern already in `launcher_loop` for exactly
  this "activation signal already sent" case. Everything else in the `ACTIVE`
  branch (`wakeword.stop_stream()`, `session_active.set()`, the
  `wait_session_done()` await) is unchanged.

`wakeword.reset()` after tail capture is required: capture consumes stream
frames that never reached `_append_to_buffer` or `model.predict`, and stale
internal state could otherwise produce an immediate false re-trigger.

## Latency

**Fast command**, measured from the moment the user stops speaking:

| | |
|---|---|
| webrtcvad trailing-silence confirm | 300 ms |
| Vosk final decode (streaming, mostly already done) | ~40 ms |
| `keyboard.send` / pycaw | ~50 ms |
| **Total** | **~0.4 s** |

Speaker verification (~200 ms) overlaps with capture and is off the critical
path.

**Cost imposed on the normal path**, per wake — never on follow-up turns:

| Case | Without optimizations | With streaming bail + `PREPARE` overlap |
|---|---|---|
| "Hey Jarvis" then a pause | +300 ms | ~0 ms (hidden behind `PREPARE`) |
| "Hey Jarvis, <long request>" one breath | +1.0 s | ~0.1–0.3 s |

### Spike result (2026-08-01) — early bail REMOVED

The spike ran against `vosk-model-small-en-us-0.15` with the starter grammar,
7 live utterances, 80 ms increments.

**Finding: partials are empty for the first ~880 ms of capture in every trace**,
in-grammar and out alike. There is no signal at the 400 ms check point to bail
on. By the time a usable partial appears (~880 ms), the 1000 ms speech cap has
essentially fired anyway, making early bail redundant at its best.

**Decision:** early bail is removed entirely — not merely disabled. Step 3 of
the capture loop, `_should_bail`, `registry.is_prefix`, and the
`FASTPATH_PARTIAL_CHECK_MS` / `FASTPATH_BAIL_ON_EMPTY_PARTIAL` constants are all
dropped. The `TAIL_MAX_SPEECH` cap does the same job more simply.

**Revised cost to the normal path** (replaces the table above):

| Case | Cost |
|---|---|
| "Hey Jarvis" then a pause | ~0 ms (300 ms lead timeout hides behind `PREPARE`) |
| "Hey Jarvis, <long request>" one breath | **~+0.7 s** (1000 ms cap − ~300 ms `PREPARE` overlap) |

Two secondary findings:

1. **`unmute` is not in the model's vocabulary.** Vosk logged `Ignoring word
   missing in vocabulary: 'unmute'` and silently dropped it from the grammar —
   the command would never have fired. Replaced with **"sound on"**. Every other
   word in the starter set was verified present. A startup vocabulary guard is
   now required (see below) so this cannot recur silently.
2. **Mid-stream partials transiently match command words inside unrelated
   speech** — "remind me to call the dentist tomorrow" produced `partial='volume'`
   then `'mute'` before settling on `[unk]`. Finals were correct 7/7. Only
   `FinalResult()` is trusted.

### Vocabulary guard

`TailRecognizer.__init__` builds a throwaway `KaldiRecognizer` over the grammar
with Vosk's stderr captured, and parses any `missing in vocabulary` warnings. Any
out-of-vocabulary word is logged at ERROR with the owning command name, so a
phrase that can never match is caught at boot rather than by silent non-response.

### Unverified: decode latency after endpointing

The ~0.4 s fast-command budget assumes `FinalResult()` returns in ~40 ms once
audio is already fed. The spike measured *partial* lag (~880 ms), which is a
different code path, so this is not yet confirmed. Task 10's live `total_ms` log
line measures it. If `FinalResult()` proves slow, the fast-command target moves
and `FASTPATH_TRAILING_SILENCE_MS` is the first knob to tune.

## Fall-through cost

When the tail contains speech that is not a fast command, that speech is lost —
the user repeats their request after the greeting. This is inherent to closed-
grammar recognition: the recognizer cannot produce arbitrary text to forward to
the agent. The closed grammar makes it rare, and the `TAIL_MAX_SPEECH` cap means
a genuinely long request is abandoned before the user finishes saying it, so in
practice the loss applies to short non-command utterances only.

## Failure handling

Every failure degrades to today's behavior. None of them can crash the launcher.

| Failure | Behavior |
|---|---|
| Vosk model missing or download failed | `available` is `False`; `capture_and_match` returns `None` without touching the stream. Launcher behaves exactly as today. |
| Vosk import fails | Same as above, caught at construction. |
| Decode returns `[unk]` / empty | Normal activation. |
| Speech cap hit | Normal activation, no decode. |
| Action raises | Log the exception, play an error beep, return to `SLEEPING`. No session started. |
| Stream read error during capture | Log, return `None`, fall through to normal activation. |
| Agent subprocess dead when `PREPARE` is sent | Existing respawn path in the `SLEEPING` branch runs first, unchanged. |

## Observability

One log line per wake at INFO:

```
fastpath: matched=%s tail_ms=%.0f speech_ms=%.0f vosk_ms=%.0f total_ms=%.0f
```

`matched` is the command name or `-`. This makes the latency claims measurable
rather than assumed, and makes tuning the five constants data-driven.

## Testing

pytest (already a dependency; `tests/` already contains
`test_open_on_screen.py` and `test_screen_scroll.py`).

- **`tests/test_fastpath_registry.py`** — no phrase collides across commands;
  every command's action is callable; `grammar_phrases()` covers every phrase in
  `COMMANDS` and includes `[unk]`; `lookup` normalizes case, surrounding
  whitespace, and trailing punctuation; `lookup("[unk]")` and `lookup("")`
  return `None`.
- **`tests/test_fastpath_recognizer.py`** — a fake stream object yielding
  scripted 1280-sample int16 chunks, with Vosk stubbed by a fake recognizer
  returning canned JSON. Asserts: silence-only tail returns `None` within the
  lead timeout; trailing silence ends capture; the speech cap aborts without
  decoding; a matching decode returns the right `FastCommand`; a stream read
  raising returns `None` rather than propagating.
- **`tests/test_fastpath_actions.py`** — monkeypatch `keyboard.send` and the
  pycaw endpoint helpers; assert each action issues exactly the expected call.
  Also asserts `friday.tools.media`'s MCP wrappers delegate to the same
  module-level functions, so the two paths cannot drift.

Manual verification: say each command in the starter set, confirm the action
fires and read the `fastpath:` timing line from `logs/friday.log`.

## Dependencies

- New: `vosk` in `pyproject.toml`.
- Already present: `webrtcvad` (used by `friday/webrtc_vad.py`, though it is
  currently missing from `pyproject.toml` — add it while editing that file),
  `pyaudio`, `keyboard`, `pycaw`, `comtypes`.
- New asset: `sounds/fastpath_ack.wav`, a short tick distinct from
  `activate.wav`.
- One-time download: `models/vosk-model-small-en-us-0.15/` (~40 MB), gitignored.

## Documentation

`ARCHITECTURE.md` is updated in the same change: the launcher process diagram
gains the fast-path branch and the `PREPARE`/`GREET`/`ABORT` protocol, and the
Feature Map gains a fast-path row. Required by `CLAUDE.md`.
