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
