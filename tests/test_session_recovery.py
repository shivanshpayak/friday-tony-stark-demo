"""Tests for STT session-death recovery logic."""
from friday.recovery import is_fatal_close, should_announce_recovery


def test_error_close_is_fatal():
    assert is_fatal_close("error", intentional=False) is True


def test_intentional_error_close_is_not_fatal():
    # We set the intentional flag before our own aclose(); never recover from it.
    assert is_fatal_close("error", intentional=True) is False


def test_user_initiated_close_is_not_fatal():
    assert is_fatal_close("user_initiated", intentional=False) is False


def test_job_shutdown_close_is_not_fatal():
    assert is_fatal_close("job_shutdown", intentional=False) is False


def test_session_conn_options_uses_configured_stt_tolerance():
    from friday.providers import build_session_conn_options
    from friday.config import STT_MAX_RETRY, STT_TIMEOUT

    opts = build_session_conn_options()
    assert opts.stt_conn_options.max_retry == STT_MAX_RETRY
    assert opts.stt_conn_options.timeout == STT_TIMEOUT


def _patch_and_import_launcher():
    import sys, types
    for name, attrs in {
        "winsound": {},
        "numpy": {"frombuffer": lambda *a, **k: None, "int16": None, "ndarray": object},
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


def test_should_announce_recovery_only_when_active():
    assert should_announce_recovery(session_active=True) is True
    assert should_announce_recovery(session_active=False) is False


def test_recovery_cue_gated_on_active_session():
    """A fatal close mid-conversation queues the cue; an idle one does not."""
    import threading
    fl = _patch_and_import_launcher()
    session_active = threading.Event()
    pending = threading.Event()

    def on_fatal():
        if should_announce_recovery(session_active=session_active.is_set()):
            pending.set()

    agent = fl.AgentProcess(on_fatal=on_fatal)

    # Idle death (no active session) -> silent recovery, no cue.
    agent._dispatch_line("SESSION_FATAL")
    assert not pending.is_set()

    # Mid-conversation death -> cue queued.
    session_active.set()
    agent._dispatch_line("SESSION_FATAL")
    assert pending.is_set()


# ---------------------------------------------------------------------------
# Regression (logs 2026-08-01 → 2026-09-16, ~20 occurrences): after
# SESSION_FATAL the agent's job shut down but its PROCESS stayed alive, so the
# launcher's `if not agent.alive` respawn check never fired. Every later wake
# word was sent to a dead agent ("Executor shutdown has been called") until the
# user pressed the restart hotkey.
# ---------------------------------------------------------------------------

class _FakeProc:
    """A subprocess stand-in that stays 'alive' (poll() is None)."""
    def __init__(self):
        self.stdin = _FakeStdin()
        self.pid = 999999
    def poll(self):
        return None


def test_fatal_line_unblocks_session_wait():
    """A fatal mid-conversation must release the launcher's wait_session_done(),
    which otherwise only a SESSION_DONE (never sent after a fatal) can release."""
    fl = _patch_and_import_launcher()
    agent = fl.AgentProcess()
    agent._proc = _FakeProc()
    agent._dispatch_line("SESSION_FATAL")
    assert agent._session_done.is_set()


def test_fatal_marks_agent_for_respawn_even_if_process_lingers():
    fl = _patch_and_import_launcher()
    agent = fl.AgentProcess()
    agent._proc = _FakeProc()
    assert agent.alive and not agent.needs_respawn
    agent._dispatch_line("SESSION_FATAL")
    assert agent.alive, "precondition: the process is still running"
    assert agent.needs_respawn


def test_dead_process_needs_respawn():
    fl = _patch_and_import_launcher()
    agent = fl.AgentProcess()
    assert agent.needs_respawn  # never started


def test_start_clears_fatal_flag(monkeypatch):
    fl = _patch_and_import_launcher()
    agent = fl.AgentProcess()
    agent._proc = _FakeProc()
    agent._dispatch_line("SESSION_FATAL")

    class _Spawned(_FakeProc):
        stdout = iter(())
    monkeypatch.setattr(fl.subprocess, "Popen", lambda *a, **k: _Spawned())
    agent._proc = None
    agent.start()
    assert not agent.needs_respawn


def test_respawn_backoff_grows_and_caps():
    from friday.recovery import respawn_backoff
    assert respawn_backoff(0) == 0
    delays = [respawn_backoff(n) for n in range(1, 10)]
    assert delays == sorted(delays), "backoff must never shrink"
    assert delays[0] > 0
    assert max(delays) <= 30


def test_boot_with_retries_retries_then_succeeds():
    """Cold boot on 2026-09-20 missed the 60s window once and the launcher
    exited for good. A failed boot must be retried, not fatal."""
    import asyncio
    fl = _patch_and_import_launcher()
    results = iter([False, False, True])
    slept = []

    class _Agent:
        calls = 0
        async def boot(self, timeout):
            _Agent.calls += 1
            return next(results)

    async def fake_sleep(s):
        slept.append(s)

    ok = asyncio.run(fl.boot_with_retries(_Agent(), attempts=3, timeout=1.0,
                                          sleep=fake_sleep))
    assert ok is True
    assert _Agent.calls == 3
    assert len(slept) == 2 and all(s > 0 for s in slept)


def test_boot_with_retries_gives_up_after_attempts():
    import asyncio
    fl = _patch_and_import_launcher()

    class _Agent:
        async def boot(self, timeout):
            return False

    async def fake_sleep(s):
        pass

    assert asyncio.run(fl.boot_with_retries(_Agent(), attempts=2, timeout=1.0,
                                            sleep=fake_sleep)) is False


def test_session_conn_options_bounds_llm_retries():
    """Gemini returns an empty completion when the prompt tells it to stay
    silent after a tool succeeds; the plugin treats that as a retryable error.
    With livekit's default (3 retries, 2s apart) every silent turn cost ~6s of
    dead air in which the user could not be heard. One fast retry is enough."""
    from friday.providers import build_session_conn_options
    from friday.config import LLM_MAX_RETRY

    opts = build_session_conn_options()
    assert opts.llm_conn_options.max_retry == LLM_MAX_RETRY
    assert LLM_MAX_RETRY <= 1


def test_agent_process_exits_after_activation_loop():
    """Source guard: console mode keeps the process alive after ctx.shutdown(),
    so the entrypoint must exit the process itself once its loop ends —
    otherwise a fatal close (or launcher EOF) leaves a zombie agent."""
    import re
    from pathlib import Path
    import agent_friday

    src = Path(agent_friday.__file__).read_text(encoding="utf-8")
    tail = src[src.index("ctx.shutdown("):src.index("# CLI")]
    assert re.search(r"os\._exit\(", tail), (
        "entrypoint must os._exit() after ctx.shutdown() — console mode will "
        "not end the process on its own"
    )


def test_quick_death_after_boot_counts_as_failure():
    """Network down at boot (log 2026-09-15): the agent reaches FRIDAY_READY,
    then Deepgram fails ~1s later. A boot that dies that fast must grow the
    backoff, or the launcher respawns in a tight loop until the network returns."""
    from friday.recovery import STABLE_UPTIME, next_respawn_failures
    assert next_respawn_failures(0, uptime=5.0) == 1
    assert next_respawn_failures(3, uptime=STABLE_UPTIME - 1) == 4


def test_death_after_stable_uptime_resets_backoff():
    from friday.recovery import STABLE_UPTIME, next_respawn_failures
    assert next_respawn_failures(4, uptime=STABLE_UPTIME + 1) == 0
