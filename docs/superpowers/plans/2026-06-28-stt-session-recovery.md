# STT Session-Death Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an unrecoverable STT/connection error closes FRIDAY's long-lived `AgentSession`, the agent exits cleanly and the launcher respawns it (speaking a recovery line if it died mid-conversation), instead of lingering deaf to PTT and wake word.

**Architecture:** Three parts. (A) Prevention: raise STT retry/timeout tolerance via `SessionConnectOptions` so brief blips don't kill the session. (B) Agent: detect a `CloseReason.ERROR` close, print `SESSION_FATAL`, and break the activation loop so the process exits through its normal cleanup. (C) Launcher: the existing respawn-on-death path recovers it; on `SESSION_FATAL` it auto-reactivates the respawned agent with a recovery line via a new `START_RECOVERED` command.

**Tech Stack:** Python, asyncio, livekit-agents (console mode), pytest. Spec: [docs/superpowers/specs/2026-06-28-stt-session-recovery-design.md](../specs/2026-06-28-stt-session-recovery-design.md).

---

## File Structure

- `friday/recovery.py` — **new**, import-light pure helper: `is_fatal_close(reason, intentional)`. No livekit/audio imports so it's trivially testable.
- `friday/config.py` — **modify**: add `STT_MAX_RETRY`, `STT_TIMEOUT`, and `RECOVERY_LINE_INSTRUCTIONS`.
- `friday/providers.py` — **modify**: add `build_session_conn_options()` returning a `SessionConnectOptions`.
- `agent_friday.py` — **modify**: wire `conn_options`, add the recovery ready-line, the close handler + fatal flags + loop break, and `START_RECOVERED` handling.
- `friday_launcher.py` — **modify**: recognise `SESSION_FATAL`, add `send_start_recovered()`, and fire the recovery cue after respawn.
- `tests/test_session_recovery.py` — **new**: unit tests for the pure helpers and the launcher command/parse logic.

Verification commands assume the project venv: prefix with `.venv/Scripts/python.exe -m`.

---

### Task 1: Pure helper — `is_fatal_close`

**Files:**
- Create: `friday/recovery.py`
- Test: `tests/test_session_recovery.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_recovery.py`:

```python
"""Tests for STT session-death recovery logic."""
from friday.recovery import is_fatal_close


def test_error_close_is_fatal():
    assert is_fatal_close("error", intentional=False) is True


def test_intentional_error_close_is_not_fatal():
    # We set the intentional flag before our own aclose(); never recover from it.
    assert is_fatal_close("error", intentional=True) is False


def test_user_initiated_close_is_not_fatal():
    assert is_fatal_close("user_initiated", intentional=False) is False


def test_job_shutdown_close_is_not_fatal():
    assert is_fatal_close("job_shutdown", intentional=False) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.recovery'`

- [ ] **Step 3: Write minimal implementation**

Create `friday/recovery.py`:

```python
"""Pure decision helpers for session-death recovery.

Kept dependency-free (no livekit/audio imports) so it is trivially unit-testable
and importable from both the agent and tests.
"""


def is_fatal_close(reason, *, intentional: bool) -> bool:
    """True when a session close should make the agent exit for a launcher respawn.

    `reason` is a livekit CloseReason (a str-Enum) or its string value. Only an
    unrecoverable ERROR close that we did NOT initiate ourselves is fatal.
    """
    return str(reason) == "error" and not intentional
```

Note: `CloseReason` is `class CloseReason(str, Enum)` with `ERROR = "error"`, so both the enum member and `str(member)` compare equal to `"error"`. (`str(CloseReason.ERROR)` is `"CloseReason.ERROR"` on some Python versions — guard against that in the next step.)

- [ ] **Step 4: Harden against enum repr, re-run**

Replace the comparison so it works whether `reason` is the enum, its `.value`, or a bare string:

```python
def is_fatal_close(reason, *, intentional: bool) -> bool:
    value = getattr(reason, "value", reason)
    return value == "error" and not intentional
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add friday/recovery.py tests/test_session_recovery.py
git commit -m "feat(recovery): add is_fatal_close helper for session-death detection"
```

---

### Task 2: Config — STT tolerance + recovery line text

**Files:**
- Modify: `friday/config.py`

- [ ] **Step 1: Add the constants**

Add near the other `STT_*` settings in `friday/config.py` (after line 16, `STT_PROVIDER = "groq"`):

```python
# --- STT connection tolerance (defense-in-depth against transient blips) ---
# livekit defaults are max_retry=3, timeout=10.0; a brief network blip on the
# Groq STT path exhausts those in ~30s and kills the session. Widen them so
# short outages are ridden out before any close happens.
STT_MAX_RETRY = 6
STT_TIMEOUT   = 15.0
```

Add near the end of the prompt/text constants (anywhere top-level is fine):

```python
# Spoken when the agent comes back after a fatal STT/connection drop forced a
# restart. One short line acknowledging the drop, in FRIDAY's voice.
RECOVERY_LINE_INSTRUCTIONS = (
    "You just reconnected after briefly losing the connection. "
    "Reply with exactly one short, calm, professional sentence acknowledging "
    "the drop and that you are back. Example: 'I lost the connection for a moment, sir — back now.' "
    "Never use his name. Do NOT mention errors, networks, restarts, or tools. Do NOT call any tools."
)
```

- [ ] **Step 2: Verify it imports**

Run: `.venv/Scripts/python.exe -c "from friday.config import STT_MAX_RETRY, STT_TIMEOUT, RECOVERY_LINE_INSTRUCTIONS; print(STT_MAX_RETRY, STT_TIMEOUT)"`
Expected: `6 15.0`

- [ ] **Step 3: Commit**

```bash
git add friday/config.py
git commit -m "feat(config): add STT retry/timeout tolerance and recovery line text"
```

---

### Task 3: Prevention — `build_session_conn_options` + wire into AgentSession

**Files:**
- Modify: `friday/providers.py`
- Modify: `agent_friday.py:446-470` (AgentSession construction)
- Test: `tests/test_session_recovery.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_recovery.py`:

```python
def test_session_conn_options_uses_configured_stt_tolerance():
    from friday.providers import build_session_conn_options
    from friday.config import STT_MAX_RETRY, STT_TIMEOUT

    opts = build_session_conn_options()
    assert opts.stt_conn_options.max_retry == STT_MAX_RETRY
    assert opts.stt_conn_options.timeout == STT_TIMEOUT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py::test_session_conn_options_uses_configured_stt_tolerance -v`
Expected: FAIL — `ImportError: cannot import name 'build_session_conn_options'`

- [ ] **Step 3: Implement the builder**

In `friday/providers.py`, add the imports at the top (next to the existing `from livekit.plugins import ...`):

```python
from livekit.agents import APIConnectOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from .config import STT_MAX_RETRY, STT_TIMEOUT
```

(Extend the existing `from .config import (...)` block rather than duplicating it if you prefer — both `STT_MAX_RETRY` and `STT_TIMEOUT` must be imported.)

Then add the factory at the end of `friday/providers.py`:

```python
def build_session_conn_options() -> SessionConnectOptions:
    """Connection options for the AgentSession.

    Widens STT retry/timeout beyond livekit defaults (max_retry=3, timeout=10)
    so a brief blip on the STT path is ridden out instead of closing the session.
    """
    return SessionConnectOptions(
        stt_conn_options=APIConnectOptions(
            max_retry=STT_MAX_RETRY,
            timeout=STT_TIMEOUT,
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py::test_session_conn_options_uses_configured_stt_tolerance -v`
Expected: PASS

- [ ] **Step 5: Wire it into the AgentSession**

In `agent_friday.py`, extend the providers import at line 63:

```python
from friday.providers import build_stt, build_llm, build_tts, build_session_conn_options
```

Then in the `AgentSession(...)` call at `agent_friday.py:446`, add the `conn_options` argument (place it right after `aec_warmup_duration=0.0,`):

```python
    session = AgentSession(
        aec_warmup_duration=0.0,
        conn_options=build_session_conn_options(),
        turn_handling=TurnHandlingOptions(
```

- [ ] **Step 6: Verify the module still imports**

Run: `.venv/Scripts/python.exe -c "import agent_friday; print('import ok')"`
Expected: `import ok` (no exceptions)

- [ ] **Step 7: Commit**

```bash
git add friday/providers.py agent_friday.py tests/test_session_recovery.py
git commit -m "feat(recovery): widen STT retry/timeout via SessionConnectOptions"
```

---

### Task 4: Agent — recovery ready-line + START_RECOVERED command

**Files:**
- Modify: `agent_friday.py` (import recovery line; `_stdin_dispatch_loop` ~L343; activation loop ~L690-714)

- [ ] **Step 1: Import the recovery line text**

Extend the `from friday.config import (...)` block at `agent_friday.py:58-62` to include `RECOVERY_LINE_INSTRUCTIONS`:

```python
from friday.config import (
    SYSTEM_PROMPT, DISMISSAL_PHRASES, SLEEP_RESPONSES,
    STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    MAX_HISTORY_ITEMS, SESSION_SPEAKER_GATE_MAX_REJECTS, logger,
    RECOVERY_LINE_INSTRUCTIONS,
)
```

- [ ] **Step 2: Accept START_RECOVERED in the stdin dispatcher**

In `_stdin_dispatch_loop`, change the queue-routing line at `agent_friday.py:343`:

```python
        elif cmd in ("START", "START_RECOVERED", "QUIT"):
            await cmd_queue.put(cmd)
```

- [ ] **Step 3: Branch the ready line on recovery in the activation loop**

In the activation loop, replace the command check and the ready-line generation. Current code (`agent_friday.py:690-714`):

```python
        cmd = await cmd_queue.get()
        if cmd != "START":
            logger.info("Received %r — shutting down", cmd or "EOF")
            break
        ...
        try:
            await session.generate_reply(
                instructions=_READY_LINE_INSTRUCTIONS,
                tool_choice="none",
            )
            logger.info("Re-activation ready line completed")
        except Exception as e:
            logger.warning("Re-activation ready line failed: %s", e)
```

becomes:

```python
        cmd = await cmd_queue.get()
        if cmd not in ("START", "START_RECOVERED"):
            logger.info("Received %r — shutting down", cmd or "EOF")
            break
        recovered = cmd == "START_RECOVERED"
        ...
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
```

(The `...` lines between — `_signing_off = False`, `dismissed.clear()`, gate resets, `print("SESSION_STARTED", ...)`, and the `_refresh_stt_streams` block — stay exactly as they are.)

- [ ] **Step 4: Verify the module still imports**

Run: `.venv/Scripts/python.exe -c "import agent_friday; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add agent_friday.py
git commit -m "feat(recovery): add START_RECOVERED command + recovery ready-line"
```

---

### Task 5: Agent — detect fatal close, signal, and exit

**Files:**
- Modify: `agent_friday.py` (import `CloseReason` + `is_fatal_close`; flags near `dismissed` ~L555; close handler after `cmd_queue` ~L685; loop break ~L731; intentional flag before cleanup ~L740)

- [ ] **Step 1: Add imports**

Extend `agent_friday.py:54` and add the recovery import after the providers import (line 63):

```python
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.events import CloseReason  # noqa: F401  (documents the reason values)
```

and after `from friday.speaker_gate import get_speaker_gate` (line 65):

```python
from friday.recovery import is_fatal_close
```

- [ ] **Step 2: Add the fatal-state holders**

Right after `dismissed.set()` at `agent_friday.py:556`, add two one-element list holders (lists avoid `nonlocal` gymnastics inside the sync event callback):

```python
    # Fatal-close recovery state. `_fatal[0]` is raised by the close handler when
    # an unrecoverable STT/connection error closes the session; `_intentional[0]`
    # is raised before our OWN aclose()/QUIT so the handler ignores those.
    _fatal = [False]
    _intentional = [False]
```

- [ ] **Step 3: Register the close handler**

Right after the stdin dispatcher is started at `agent_friday.py:685` (`asyncio.create_task(_stdin_dispatch_loop(session, cmd_queue))`), add:

```python
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
```

- [ ] **Step 4: Break the loop on fatal after the ACTIVE wait**

In the activation loop, after `await dismissed.wait()` at `agent_friday.py:731`, add a fatal check before the mic-gating cleanup. Current:

```python
        # Stay active until user dismisses ("that'll be all", etc.)
        await dismissed.wait()
        logger.info("Session dismissed — gating mic")
```

becomes:

```python
        # Stay active until user dismisses ("that'll be all", etc.) or a fatal close.
        await dismissed.wait()
        if _fatal[0]:
            logger.info("Fatal close — breaking activation loop for clean exit")
            break
        logger.info("Session dismissed — gating mic")
```

- [ ] **Step 5: Mark intentional before our own teardown**

At the cleanup block at `agent_friday.py:740-749`, raise the intentional flag first so any aclose-triggered close event is ignored:

```python
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
```

- [ ] **Step 6: Verify the module imports**

Run: `.venv/Scripts/python.exe -c "import agent_friday; print('import ok')"`
Expected: `import ok`

- [ ] **Step 7: Run the full unit suite (no regressions)**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all tests in `test_session_recovery.py` and `test_state_machine.py` PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_friday.py
git commit -m "feat(recovery): exit agent process on fatal STT close, emit SESSION_FATAL"
```

---

### Task 6: Launcher — recognise SESSION_FATAL + send START_RECOVERED + recovery cue

**Files:**
- Modify: `friday_launcher.py` (`AgentProcess.__init__` ~L449; `_read_stdout` ~L531-554; new `send_start_recovered`; respawn block ~L749-758)
- Test: `tests/test_session_recovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_recovery.py` (reuse the heavy-import patch pattern from `test_state_machine.py` so `friday_launcher` imports in a bare environment):

```python
def _patch_and_import_launcher():
    import sys, types
    for name, attrs in {
        "winsound": {},
        "numpy": {"frombuffer": lambda *a, **k: None, "int16": None},
        "pyaudio": {"PyAudio": object, "paInt16": 8},
        "dotenv": {"load_dotenv": lambda *a, **k: None},
    }.items():
        if name not in sys.modules:
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[name] = m
    if "openwakeword" not in sys.modules:
        ow = types.ModuleType("openwakeword")
        owm = types.ModuleType("openwakeword.model")
        owm.Model = object
        sys.modules["openwakeword"] = ow
        sys.modules["openwakeword.model"] = owm
    import friday_launcher
    return friday_launcher


class _FakeStdin:
    def __init__(self):
        self.written = []
    def write(self, s):
        self.written.append(s)
    def flush(self):
        pass


def test_send_start_recovered_writes_command():
    fl = _patch_and_import_launcher()
    agent = fl.AgentProcess()
    fake = _FakeStdin()
    # Pretend the subprocess is alive with a writable stdin.
    agent._proc = type("P", (), {"stdin": fake, "poll": lambda self: None})()
    agent.send_start_recovered()
    assert agent._proc.stdin.written == ["START_RECOVERED\n"]


def test_fatal_line_sets_recovery_flag():
    fl = _patch_and_import_launcher()
    calls = []
    agent = fl.AgentProcess(on_fatal=lambda: calls.append(True))
    agent._dispatch_line("SESSION_FATAL")
    assert calls == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py::test_send_start_recovered_writes_command tests/test_session_recovery.py::test_fatal_line_sets_recovery_flag -v`
Expected: FAIL — `AgentProcess.__init__` has no `on_fatal`; no `_dispatch_line`; no `send_start_recovered`.

- [ ] **Step 3: Add `on_fatal` to AgentProcess.__init__**

In `friday_launcher.py:449`:

```python
    def __init__(self, on_processing=None, on_speaking=None, on_listening=None,
                 on_fatal=None):
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._session_done = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._on_processing = on_processing  # callback when LLM starts thinking
        self._on_speaking = on_speaking       # callback when first TTS content arrives
        self._on_listening = on_listening     # callback when mic is live again
        self._on_fatal = on_fatal             # callback on SESSION_FATAL (session died)
```

- [ ] **Step 4: Extract `_dispatch_line` and recognise SESSION_FATAL**

Refactor `_read_stdout` (`friday_launcher.py:525-554`) so the per-line dispatch is a separately testable method. Replace the body of the `for line in self._proc.stdout:` loop with a call to `self._dispatch_line(line.rstrip())`, and add the new method:

```python
    def _read_stdout(self):
        """Drain subprocess stdout, dispatch signals, echo everything else."""
        try:
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                self._dispatch_line(line.rstrip())
        except Exception as e:
            logger.debug("stdout reader error: %s", e)
        finally:
            self._ready.set()
            self._session_done.set()

    def _dispatch_line(self, stripped: str):
        if "FRIDAY_READY" in stripped:
            logger.info("Agent subprocess signalled FRIDAY_READY")
            self._ready.set()
        elif "SESSION_FATAL" in stripped:
            logger.error("Agent subprocess signalled SESSION_FATAL — session died, will respawn")
            if self._on_fatal:
                self._on_fatal()
        elif "SESSION_STARTED" in stripped:
            logger.info("Agent subprocess signalled SESSION_STARTED")
        elif "SESSION_LISTENING" in stripped:
            logger.info("Agent subprocess signalled SESSION_LISTENING")
            if self._on_listening:
                self._on_listening()
        elif "SESSION_DONE" in stripped:
            logger.info("Agent subprocess signalled SESSION_DONE")
            self._session_done.set()
        elif "PROCESSING" in stripped:
            if self._on_processing:
                self._on_processing()
        elif "SPEAKING" in stripped:
            if self._on_speaking:
                self._on_speaking()
        elif stripped:
            if any(p in stripped for p in self._NOISE_PATTERNS):
                return
            logging.getLogger("friday-agent").info(stripped)
```

Note: `SESSION_FATAL` is checked before `SESSION_STARTED`/`SESSION_DONE`. Because `"SESSION_DONE" in stripped` etc. are substring checks, ordering matters only if one signal's text contains another's — none do — but keeping `SESSION_FATAL` early is clearest.

- [ ] **Step 5: Add `send_start_recovered`**

After `send_start` (`friday_launcher.py:567-578`), add:

```python
    def send_start_recovered(self):
        """Begin a session that opens with the recovery line (post-crash respawn)."""
        if not self.alive:
            logger.warning("Cannot send START_RECOVERED — agent subprocess is dead")
            return
        self._session_done.clear()
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write("START_RECOVERED\n")
            self._proc.stdin.flush()
        except Exception as e:
            logger.error("Failed to write START_RECOVERED: %s", e)
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_recovery.py -v`
Expected: PASS (all, including the two new ones)

- [ ] **Step 7: Wire the recovery cue into the launcher loop**

In `launcher_loop`, construct the `AgentProcess` with an `on_fatal` that raises a pending flag. Replace the `agent = AgentProcess(...)` at `friday_launcher.py:640-644`:

```python
    pending_recovery_cue = threading.Event()
    agent = AgentProcess(
        on_processing=lambda: overlay.show_loading("Thinking..."),
        on_speaking=lambda: overlay.hide_loading(),
        on_listening=lambda: overlay.show(),
        on_fatal=lambda: pending_recovery_cue.set(),
    )
```

Then in the SLEEPING respawn block (`friday_launcher.py:749-758`), after the agent is back and ready, fire the cue. Replace:

```python
                if not agent.alive:
                    logger.warning("Agent subprocess died — respawning…")
                    overlay.show_loading("Rebooting JARVIS...")
                    wakeword.stop_stream()
                    agent.start()
                    await agent.wait_ready(timeout=60.0)
                    overlay.hide()
                    wakeword.start_stream()
```

with:

```python
                if not agent.alive:
                    logger.warning("Agent subprocess died — respawning…")
                    overlay.show_loading("Rebooting JARVIS...")
                    wakeword.stop_stream()
                    agent.start()
                    await agent.wait_ready(timeout=60.0)
                    overlay.hide()
                    wakeword.start_stream()
                    # If the death was a fatal session close (SESSION_FATAL),
                    # come back speaking — re-activate with the recovery line
                    # instead of waiting silently for the next wake word.
                    if pending_recovery_cue.is_set() and agent.alive:
                        pending_recovery_cue.clear()
                        logger.info("Recovering from fatal close — re-activating with recovery line")
                        play_activation_ack()
                        overlay.show_loading("Reconnecting...")
                        agent.send_start_recovered()
                        state = State.ACTIVE
                        logger.info("State → ACTIVE (recovery)")
                        continue
```

Note: the `continue` re-enters the loop in `ACTIVE`, where the existing branch (`friday_launcher.py:797`) calls `agent.send_start()` — but we have already sent `START_RECOVERED`. To avoid a double activation, guard the ACTIVE branch's send (next step).

- [ ] **Step 8: Guard the ACTIVE branch against double-send**

The ACTIVE branch unconditionally calls `agent.send_start()`. After a recovery `continue`, the session is already starting. Track it with a flag. Just before `state = State.ACTIVE` in the recovery block (previous step), set a local `recovery_started = True`; initialise `recovery_started = False` once before the `while True:` loop (near `state = State.SLEEPING` at `friday_launcher.py:744`). Then in the ACTIVE branch at `friday_launcher.py:804`, replace:

```python
                agent.send_start()
```

with:

```python
                if recovery_started:
                    recovery_started = False   # START_RECOVERED already sent
                else:
                    agent.send_start()
```

And add `recovery_started = True` inside the recovery block from Step 7 (immediately before `state = State.ACTIVE`).

- [ ] **Step 9: Verify launcher imports and tests still pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add friday_launcher.py tests/test_session_recovery.py
git commit -m "feat(recovery): respawn-and-reannounce agent on SESSION_FATAL"
```

---

### Task 7: Manual integration verification

**Files:** none (runtime verification by the user).

- [ ] **Step 1: Cold-path verification (idle death)**

Start FRIDAY (`uv run friday_start`). Once "JARVIS launcher ready", disable the network adapter (so the STT endpoint is unreachable) and leave FRIDAY idle. Watch `logs/friday.log`.
Expected: STT retries (now up to `STT_MAX_RETRY`), then `SESSION_FATAL`, then `Agent subprocess died — respawning…`, then a fresh `FRIDAY_READY`. Re-enable the network; confirm wake word / PTT work again.

- [ ] **Step 2: Warm-path verification (mid-conversation death)**

Start FRIDAY, activate it (PTT), and while it is ACTIVE disable the network. 
Expected: `SESSION_FATAL` → respawn → the recovery block fires `START_RECOVERED` → FRIDAY speaks the recovery line (e.g. *"I lost the connection for a moment, sir — back now."*) after reboot. Re-enable network; confirm normal conversation resumes.

- [ ] **Step 3: No-false-positive verification**

Start FRIDAY, activate, and dismiss normally ("that'll be all"). 
Expected: normal `SESSION_DONE`, NO `SESSION_FATAL`, NO respawn, NO recovery line. Quitting the launcher (kill switch) must also not emit `SESSION_FATAL`.

- [ ] **Step 4: Record results**

Note the observed log lines for each path in the PR description. If any path misbehaves, return to systematic-debugging before claiming completion.

---

## Self-Review

**Spec coverage:**
- Prevention (STT tolerance) → Tasks 2, 3. ✓
- Detection on `CloseReason.ERROR` + intentional suppression → Task 1 (`is_fatal_close`), Task 5. ✓
- Graceful exit via existing cleanup → Task 5 (Steps 4-5). ✓
- Launcher respawn (already exists) + SESSION_FATAL recognition → Task 6. ✓
- Audible recovery cue via START_RECOVERED → Tasks 4, 6. ✓
- Silent idle recovery → Task 6 only fires the cue when `pending_recovery_cue` is set (i.e. SESSION_FATAL seen); a plain process death without it respawns silently. ✓
- Testing (pure helpers + launcher parse/command) → Tasks 1, 3, 6; manual integration → Task 7. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✓

**Type/name consistency:** `is_fatal_close(reason, *, intentional)` used identically in Task 1 and Task 5. `SESSION_FATAL`, `START_RECOVERED`, `RECOVERY_LINE_INSTRUCTIONS`, `STT_MAX_RETRY`, `STT_TIMEOUT`, `build_session_conn_options`, `send_start_recovered`, `_dispatch_line`, `on_fatal`, `pending_recovery_cue`, `recovery_started` are spelled consistently across tasks. ✓
