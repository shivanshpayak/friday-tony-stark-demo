"""Launcher ↔ agent overlay signals, and the restart hotkey's relaunch guard.

llm_node prints PROCESSING (overlay → "Thinking...") and only SPEAKING clears
it. The system prompt tells the model to stay SILENT after a successful action,
so those turns never print SPEAKING and the overlay sat on "Thinking..." while
JARVIS was actually idle and listening. A turn that ends with no output must
say so (TURN_IDLE) so the overlay returns to listening.
"""
import asyncio
import types

import pytest

import agent_friday
from livekit.agents import llm as lk_llm
from tests.test_session_recovery import _patch_and_import_launcher


def _run_llm_node(monkeypatch, fake_gen):
    monkeypatch.setattr(agent_friday.Agent.default, "llm_node", fake_gen)
    agent = object.__new__(agent_friday.FridayAgent)
    ctx = types.SimpleNamespace(items=[])

    async def drain():
        return [c async for c in agent.llm_node(ctx, [], None)]

    return asyncio.run(drain())


def test_silent_turn_signals_idle(monkeypatch, capsys):
    async def silent(agent, chat_ctx, tools, model_settings):
        return
        yield  # pragma: no cover — makes this an async generator

    _run_llm_node(monkeypatch, silent)
    out = capsys.readouterr().out
    assert "TURN_IDLE" in out


def test_spoken_turn_does_not_signal_idle(monkeypatch, capsys):
    async def spoken(agent, chat_ctx, tools, model_settings):
        yield lk_llm.ChatChunk(
            id="x", delta=lk_llm.ChoiceDelta(role="assistant", content="Done, sir.")
        )

    chunks = _run_llm_node(monkeypatch, spoken)
    out = capsys.readouterr().out
    assert len(chunks) == 1
    assert "SPEAKING" in out
    assert "TURN_IDLE" not in out


def test_failed_turn_signals_idle(monkeypatch, capsys):
    """Retries exhausted on an empty completion: the error propagates, but the
    overlay must still leave "Thinking..."."""
    async def failing(agent, chat_ctx, tools, model_settings):
        raise RuntimeError("no response generated")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        _run_llm_node(monkeypatch, failing)
    assert "TURN_IDLE" in capsys.readouterr().out


def test_turn_idle_line_fires_idle_callback():
    fl = _patch_and_import_launcher()
    calls = []
    agent = fl.AgentProcess(on_idle=lambda: calls.append(True))
    agent._dispatch_line("TURN_IDLE")
    assert calls == [True]


# ---------------------------------------------------------------------------
# Restart hotkey: start_friday.vbs went missing from disk, but the hotkey still
# spawned `wscript.exe <missing file>` (which "succeeds"), logged "New JARVIS
# instance spawned — dying now" and killed itself. JARVIS never came back.
# ---------------------------------------------------------------------------

def test_relaunch_refuses_when_launcher_script_missing(monkeypatch, tmp_path):
    fl = _patch_and_import_launcher()
    spawned = []
    monkeypatch.setattr(fl, "SILENT_LAUNCHER", tmp_path / "missing.vbs")
    monkeypatch.setattr(fl.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert fl._spawn_relaunch() is False
    assert spawned == []


def test_relaunch_spawns_when_launcher_script_present(monkeypatch, tmp_path):
    fl = _patch_and_import_launcher()
    script = tmp_path / "start_friday.vbs"
    script.write_text("' stub", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(fl, "SILENT_LAUNCHER", script)
    monkeypatch.setattr(fl.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert fl._spawn_relaunch() is True
    assert len(spawned) == 1
