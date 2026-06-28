# STT Session-Death Recovery — Design

**Date:** 2026-06-28
**Status:** Approved (pending spec review)
**Area:** `agent_friday.py`, `friday/providers.py`, `friday_launcher.py`, `tests/`

## Problem

FRIDAY runs one long-lived `AgentSession` ([agent_friday.py:671](../../../agent_friday.py)) that the
activation loop reuses for the process lifetime. When the STT provider (Groq Whisper, via the
OpenAI-compatible plugin) cannot be reached, requests time out, the plugin gives up after 3 retries,
and LiveKit closes the `AgentSession` with `reason=ERROR`, `recoverable=False`.

Nothing in FRIDAY notices: the activation loop is blocked on `await dismissed.wait()` or
`cmd_queue.get()`. After the close, the process keeps running but the session is dead, so:

- Pressing **Ctrl+Alt+Space** (PTT) sends `START` → the loop calls `generate_reply` on a dead
  session → fails silently → loops back to waiting.
- Saying **"Hey Jarvis"** does nothing for the same reason.

Observed in [logs/friday.log](../../../logs/friday.log) on 2026-06-28 12:47–12:48: three STT
`Request timed out` retries, then `AgentSession is closing due to unrecoverable error: Connection
error.`, then `session closed {reason: error}`. The underlying trigger was intermittent network
connectivity to `api.groq.com`.

## Goal

When the session dies from an unrecoverable STT/connection error, FRIDAY automatically **rebuilds the
session in-process** — keeping the expensive warm providers, speaker gate, and tool pool — speaks a
brief cue if the user was mid-conversation, and returns to a listening state. Plus lightweight
**prevention** so short blips don't kill the session in the first place.

Non-goals: fixing the user's network; changing the wake-word/PTT paths (they work — proven earlier in
the same log); reworking provider selection.

## Approach (decisions made during brainstorming + investigation)

1. **Recovery strategy:** **launcher relaunch.** Investigation (below) showed pure in-process rebuild
   in console mode requires manipulating livekit private internals, which is too fragile. Instead the
   agent **exits the process** on a fatal close and the launcher respawns it.
2. **User feedback:** audible cue after recovery when the death happened mid-conversation; silent
   return to wake-word idle otherwise.
3. **Prevention:** add defense-in-depth STT retry/timeout tolerance so transient blips are ridden out
   before any close ever happens.

## The console-mode constraint (resolved by code investigation)

The agent runs with the `console` subcommand ([friday_launcher.py:471](../../../friday_launcher.py)),
so audio I/O is a **process-global singleton**, `AgentsConsole`, acquired on the first
`session.start()` ([cli.py:345](../../../.venv/Lib/site-packages/livekit/agents/cli/cli.py)).
Decisive facts found in the installed livekit:

- `acquire_io()` raises if already acquired, and there is **no `release_io`** — `_io_acquired` is set
  once and never reset; `_aclose_impl` never touches the console singleton.
- A naive fresh `AgentSession.start()` after a death therefore gets **no mic/speaker** (the acquire
  branch is skipped). A true in-process rebuild would require re-pointing the singleton via private
  APIs (`AgentsConsole._update_sess_io`, `_io_session`) — rejected as too coupled to livekit
  internals.

**Key enabling insight:** the launcher **already respawns the agent when the process dies**
([friday_launcher.py:749-758](../../../friday_launcher.py)). The reason FRIDAY went deaf is that on a
fatal STT close the agent process *does not exit* — LiveKit closes the `AgentSession` but the
activation loop stays blocked on `dismissed.wait()` / `cmd_queue.get()`, so `agent.alive` stays True
and the launcher never respawns. **The fix is to make the agent actually exit on a fatal close**, then
the existing respawn machinery recovers it. Recovery is a ~10–15s cold reboot (warm state lost), but
uses only stable, public interfaces (stdout signal + subprocess respawn).

## Components

### A. Prevention — STT retry/timeout tolerance (defense-in-depth)

The retries seen in the log (`attempt 1,2,3`) come from livekit's default
`APIConnectOptions(max_retry=3, timeout=10.0)`. The session reads these from
`session.conn_options.stt_conn_options` ([agent.py:406](../../../.venv/Lib/site-packages/livekit/agents/voice/agent.py)),
and `AgentSession` accepts a `conn_options: SessionConnectOptions` argument
([agent_session.py:234](../../../.venv/Lib/site-packages/livekit/agents/voice/agent_session.py)).

Lever: pass a custom `SessionConnectOptions(stt_conn_options=APIConnectOptions(max_retry=…,
timeout=…))` into `AgentSession(...)`, with values from `friday/config.py` (e.g. `STT_MAX_RETRY=6`,
`STT_TIMEOUT=15.0`). This rides out longer blips before any close. It does not replace recovery — a
true outage still exhausts retries and dies.

### B. Detection + graceful exit (agent side)

- **Detection:** register `session.on("close", handler)`; import `CloseReason` from livekit. Act only
  when `ev.reason == CloseReason.ERROR` **and** an `_intentional_close` flag is not set (the flag is
  raised before the activation loop's own `session.aclose()` and on QUIT, so normal shutdown never
  triggers recovery).
- **On fatal close:** the handler (a sync emit callback running on the session loop):
  1. logs the error,
  2. `print("SESSION_FATAL", flush=True)` (launcher signal + observability),
  3. unblocks the activation loop so it can run normal cleanup: set a `_fatal` flag, `dismissed.set()`
     (unblocks the ACTIVE wait), and `cmd_queue.put_nowait("FATAL")` (unblocks the idle wait).
- **Activation loop:** after `await dismissed.wait()` (ACTIVE branch) check `if _fatal: break`; in the
  idle branch, any command that is not `"START"`/`"START_RECOVERED"` (incl. `"FATAL"`) breaks. Break →
  existing cleanup runs: `session.aclose()` (guarded), `tool_pool.aclose()` (cleans MCP children),
  `ctx.shutdown()` → process exits.
- **Result:** process exit makes `agent.alive` False; the launcher's existing respawn
  ([friday_launcher.py:749-758](../../../friday_launcher.py)) cold-reboots the agent.

### C. Launcher — recognise the signal + recovery cue

Basic recovery needs no launcher change (the dead process is already respawned). For the **audible
cue**:

- `_read_stdout` recognises `SESSION_FATAL` → logs it and sets a `pending_recovery_cue` flag (raised
  only when a session was active, which is always true when SESSION_FATAL fires).
- The process then exits → `wait_session_done` returns → loop reaches `SLEEPING` → existing respawn
  block runs (`agent.start()` + `wait_ready`). After ready, if `pending_recovery_cue`: clear it, play
  the activation ack, `agent.send_start_recovered()`, set `state = ACTIVE`.
- `AgentProcess.send_start_recovered()` writes `START_RECOVERED\n` to the agent's stdin.
- **Agent stdin:** `_stdin_dispatch_loop` recognises `START_RECOVERED` and enqueues it; the activation
  loop treats it like `START` but uses recovery ready-line instructions —
  *"I lost the connection, sir — back now."* — for that one activation.

## Data flow

```
STT request times out repeatedly
   └─(A) extra retries/timeout — most blips ride out here, no death
   └─ true outage → LiveKit emits close{reason: ERROR}
        └─(B) on("close"): reason==ERROR and not intentional?
             ├─ print SESSION_FATAL
             ├─ set _fatal; dismissed.set(); cmd_queue<-"FATAL"
             └─ activation loop breaks → aclose/tool_pool.aclose/ctx.shutdown → PROCESS EXIT
        └─(C) launcher: agent.alive == False
             ├─ respawn agent.start() + wait_ready  (~10–15s cold boot)
             └─ pending_recovery_cue? play ack; send START_RECOVERED; state=ACTIVE
                  └─ respawned agent speaks "I lost the connection, sir — back now."
```

## Error handling

- **Intentional closes** (dismissal cleanup, QUIT, job shutdown): suppressed via `_intentional_close`
  + the `reason == ERROR` check — exit-on-fatal must not fire on these.
- **Respawn loop safety:** if the network is still down after reboot, the new session will die again
  and respawn again. This is acceptable (it keeps trying) but the cue should only fire once per
  reboot, and the respawn already runs through the normal boot path. No tight spin: each cycle pays
  the full ~15s boot + the retry tolerance from Component A.
- **Cue speech fails** (TTS also down): swallow in the agent's ready-line try/except (already present);
  the agent still ends up listening.

## Testing

Extend `tests/` (alongside `tests/test_state_machine.py`), pytest, no live network. Extract pure
helpers so the async/subprocess machinery isn't required:

- `is_fatal_close(reason, intentional) -> bool` (agent helper): ERROR + not intentional → True;
  USER_INITIATED/JOB_SHUTDOWN → False; ERROR + intentional → False.
- `build_session_conn_options()` (agent/config helper): returns `SessionConnectOptions` whose
  `stt_conn_options.max_retry`/`.timeout` equal the configured values.
- `AgentProcess.send_start_recovered()` writes exactly `START_RECOVERED\n` to a fake stdin.
- Launcher stdout parsing: feeding a `SESSION_FATAL` line sets the recovery-cue flag / invokes the
  `on_fatal` callback.

Manual integration verification (documented, run by the user): block network, confirm the agent
prints `SESSION_FATAL`, exits, is respawned, and (if mid-conversation) speaks the recovery line.

## Open questions resolved

- *In-process vs relaunch?* **Launcher relaunch via agent self-exit** — pure in-process rebuild needs
  fragile livekit private-API console re-pointing.
- *Mid-conversation UX?* Audible recovery line via `START_RECOVERED` after respawn; silent
  wake-word idle otherwise.
- *Prevention?* Yes — `SessionConnectOptions` STT retry/timeout tolerance.
