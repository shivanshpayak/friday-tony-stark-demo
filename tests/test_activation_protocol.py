"""Guards the PREPARE/GREET/ABORT handshake against stray commands.

Regression (2026-08-01, live log): the first wake word after boot stalled ~5s in
Resemblyzer's JIT-compiling first embed_utterance(). That blew past the agent's
PREPARE_WAIT_TIMEOUT, so the agent aborted and returned to the OUTER activation
wait. The launcher — still finishing verification — then sent GREET, which the
outer wait did not recognise and treated as a shutdown signal:

    Received 'GREET' — shutting down
    session closed {"reason": "user_initiated"}

The agent killed itself mid-activation. Two rules follow:

1. Only QUIT / FATAL / EOF may shut the agent down. Nothing else, ever.
2. A GREET arriving outside a handshake must still produce a session — the
   launcher is blocked in wait_session_done() and would hang otherwise.
"""
import agent_friday


def test_start_commands_pass_through():
    for cmd in ("START", "START_RECOVERED", "PREPARE"):
        assert agent_friday._normalize_activation_cmd(cmd) == cmd


def test_stray_greet_becomes_a_full_start():
    """The regression. GREET outside a handshake must activate, not shut down."""
    assert agent_friday._normalize_activation_cmd("GREET") == "START"


def test_stray_abort_is_ignored_not_fatal():
    assert agent_friday._normalize_activation_cmd("ABORT") is None


def test_shutdown_signals_are_not_treated_as_activations():
    """QUIT/FATAL are handled by the caller before this runs; make sure this
    function never turns one into an activation if that order ever changes."""
    for cmd in ("QUIT", "FATAL", "", "garbage"):
        assert agent_friday._normalize_activation_cmd(cmd) is None


def test_only_quit_fatal_and_eof_break_the_activation_loop():
    """Source guard: the outer loop must not shut down on unrecognised input.

    The original `if cmd not in (...): break` is what killed the session. Any
    return to that shape reintroduces it.
    """
    import re
    from pathlib import Path

    src = Path(agent_friday.__file__).read_text(encoding="utf-8")
    loop = src[src.index("# ---- Activation loop"):]
    guard = re.search(r"if not cmd or cmd in \(([^)]*)\):\s*\n\s*logger\.info\(\s*\n?\s*\"Received", loop)

    assert guard is not None, (
        "activation loop's shutdown guard changed shape — it must break only on "
        "falsy cmd, QUIT, or FATAL"
    )
    accepted = {t.strip().strip('\"\'') for t in guard.group(1).split(",") if t.strip()}
    assert accepted == {"QUIT", "FATAL"}, (
        f"activation loop breaks on {accepted}; only QUIT/FATAL (plus EOF) are "
        "allowed to shut the agent down"
    )
