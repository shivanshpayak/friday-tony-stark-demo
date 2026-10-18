"""How a turn ends when the LLM says nothing, and how a dismissal is answered.

Measured 2026-10-11 in logs/friday.log.1: of 104 user turns, 37 hit Gemini's
empty completion (finish=STOP, no text, no tool call). livekit retried each one
and then raised; a few in a row close the session. Most were either the turn
after a successful action — where silence is the right answer — or "Dismissed.",
which then waited out the sign-off timeout with nothing said.
"""
import asyncio
import types

import pytest
from livekit.agents import APIConnectionError, APIStatusError, StopResponse
from livekit.agents import llm as lk_llm

import agent_friday

EMPTY = dict(message="no response generated", status_code=-1, retryable=True)


def _text(content: str) -> lk_llm.ChatChunk:
    return lk_llm.ChatChunk(id="x", delta=lk_llm.ChoiceDelta(role="assistant", content=content))


def _run(monkeypatch, attempts, *, after_tool=False):
    """Run llm_node against a scripted LLM; returns (chunks, calls made)."""
    calls = []

    async def fake(agent, chat_ctx, tools, model_settings):
        step = attempts[len(calls)]
        calls.append(step)
        for item in step:
            if isinstance(item, Exception):
                raise item
            yield item

    async def no_sleep(_):
        pass

    monkeypatch.setattr(agent_friday.Agent.default, "llm_node", fake)
    monkeypatch.setattr(agent_friday.asyncio, "sleep", no_sleep)
    agent = object.__new__(agent_friday.FridayAgent)
    last = types.SimpleNamespace(type="function_call_output" if after_tool else "message")
    ctx = types.SimpleNamespace(items=[last])

    async def drain():
        return [c async for c in agent.llm_node(ctx, [], None)]

    try:
        return asyncio.new_event_loop().run_until_complete(drain()), len(calls)
    except Exception as e:
        e.calls = len(calls)
        raise


def test_silence_after_a_tool_result_ends_the_turn_without_a_retry(monkeypatch, capsys):
    chunks, calls = _run(monkeypatch, [[APIStatusError(**EMPTY)]], after_tool=True)
    assert chunks == [] and calls == 1
    assert "TURN_IDLE" in capsys.readouterr().out


def test_an_empty_reply_to_the_user_is_retried_once(monkeypatch):
    chunks, calls = _run(monkeypatch, [[APIStatusError(**EMPTY)], [_text("Opening Chrome, sir.")]])
    assert [c.delta.content for c in chunks] == ["Opening Chrome, sir."]
    assert calls == 2


def test_two_empty_replies_end_the_turn_quietly(monkeypatch, capsys):
    """Nothing to say twice is silence, not an error that can close the session."""
    chunks, calls = _run(monkeypatch, [[APIStatusError(**EMPTY)], [APIStatusError(**EMPTY)]])
    assert chunks == [] and calls == 2
    assert "TURN_IDLE" in capsys.readouterr().out


def test_a_network_error_is_retried_then_raised(monkeypatch):
    chunks, calls = _run(monkeypatch, [[APIConnectionError("blip")], [_text("Back, sir.")]])
    assert len(chunks) == 1 and calls == 2

    with pytest.raises(APIConnectionError) as failed:
        _run(monkeypatch, [[APIConnectionError("down")], [APIConnectionError("down")]])
    assert failed.value.calls == 2


def test_a_reply_that_broke_off_midway_is_not_spoken_twice(monkeypatch):
    with pytest.raises(APIConnectionError) as failed:
        _run(monkeypatch, [[_text("Opening"), APIConnectionError("cut off")], [_text("Opening Chrome.")]])
    assert failed.value.calls == 1


def test_livekit_itself_never_retries_the_llm():
    from friday.providers import build_session_conn_options
    assert build_session_conn_options().llm_conn_options.max_retry == 0


# --- thinking ---------------------------------------------------------------

def test_thinking_is_switched_off_explicitly(monkeypatch):
    """An unset thinking_config is Gemini's dynamic thinking — the setting with
    the most empty completions — so "off" has to be sent as a budget of 0."""
    from livekit.plugins import google as lk_google
    from friday import config, providers

    seen = {}
    monkeypatch.setattr(providers, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(lk_google, "LLM", lambda **kw: seen.update(kw))
    providers.build_llm(mode="fast")

    assert config.FAST_THINKING_BUDGET == 0
    assert seen["thinking_config"] == {"thinking_budget": 0}


# --- dismissal --------------------------------------------------------------

class _Session:
    def __init__(self):
        self.said = []

    def say(self, text, audio=None):
        self.said.append((text, audio))


def _dismiss(monkeypatch, transcript, lines=None):
    session = _Session()
    monkeypatch.setattr(agent_friday.FridayAgent, "session", property(lambda self: session))
    agent = object.__new__(agent_friday.FridayAgent)
    agent.signoff_lines = lines
    message = types.SimpleNamespace(text_content=transcript)
    asyncio.new_event_loop().run_until_complete(agent.on_user_turn_completed(None, message))
    return session


def test_a_dismissal_is_answered_without_the_llm(monkeypatch):
    lines = types.SimpleNamespace(pick=lambda: ("Standing watch.", ["frame"]))
    session = _Session()
    monkeypatch.setattr(agent_friday.FridayAgent, "session", property(lambda self: session))
    agent = object.__new__(agent_friday.FridayAgent)
    agent.signoff_lines = lines

    with pytest.raises(StopResponse):       # StopResponse = no LLM reply this turn
        asyncio.new_event_loop().run_until_complete(agent.on_user_turn_completed(
            None, types.SimpleNamespace(text_content="Never mind. Dismissed.")))

    assert [text for text, _ in session.said] == ["Standing watch."]
    assert session.said[0][1] is not None, "the pre-rendered audio must be replayed"


def test_a_dismissal_before_the_lines_are_rendered_is_still_spoken(monkeypatch):
    from friday.config import SLEEP_RESPONSES

    with pytest.raises(StopResponse):
        _dismiss(monkeypatch, "That'll be all.")
    session = agent_friday.FridayAgent.session.fget(None)
    assert session.said[0][0] in SLEEP_RESPONSES and session.said[0][1] is None


def test_an_ordinary_request_goes_to_the_llm(monkeypatch):
    session = _dismiss(monkeypatch, "Open Chrome.")
    assert session.said == []
