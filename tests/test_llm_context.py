"""What each completion is handed: the date and time (agent_friday._with_time),
and for small talk no tools and no earlier conversation.

Without the time the model guessed: "just another Tuesday" on a Sunday, "I
trust your morning" at 2 a.m. It rides on his latest line, not the system
instructions, so the cacheable part of the request stays the same.
"""
import asyncio
from datetime import datetime

from livekit.agents import llm as lk_llm

import agent_friday


def _ctx():
    ctx = lk_llm.ChatContext()
    ctx.add_message(role="system", content="persona")
    ctx.add_message(role="user", content="How you doing, baby?")
    return ctx


def test_time_rides_on_his_latest_line():
    ctx = agent_friday._with_time(_ctx(), now=datetime(2026, 10, 18, 1, 52))
    assert [item.role for item in ctx.items] == ["system", "user"]
    assert ctx.items[0].text_content == "persona"          # the cacheable part, untouched
    line = ctx.items[-1].text_content
    assert "Sunday, October 18, 2026, 1:52 AM" in line
    assert line.endswith("How you doing, baby?")


def test_the_session_history_is_left_alone():
    original = _ctx()
    agent_friday._with_time(original)
    assert [item.text_content for item in original.items] == ["persona", "How you doing, baby?"]


def test_after_a_tool_result_the_note_still_reaches_his_line():
    ctx = _ctx()
    ctx.items.append(lk_llm.FunctionCallOutput(call_id="1", name="t", output="ok", is_error=False))
    timed = agent_friday._with_time(ctx, now=datetime(2026, 10, 18, 1, 52))
    assert agent_friday._ends_with_tool_result(timed)
    assert "1:52 AM" in timed.items[1].text_content


def test_no_line_of_his_no_note():
    ctx = lk_llm.ChatContext()
    ctx.add_message(role="system", content="persona")
    assert agent_friday._with_time(ctx) is ctx


def _tools_seen(monkeypatch, line, tools=("a_tool",)):
    """What llm_node hands the model for a turn ending in `line`, after an
    earlier exchange."""
    seen = {}

    async def fake(agent, chat_ctx, tools, model_settings):
        seen["tools"], seen["settings"] = tools, model_settings
        seen["texts"] = [item.text_content for item in chat_ctx.items]
        yield lk_llm.ChatChunk(id="x", delta=lk_llm.ChoiceDelta(role="assistant", content="Sir."))

    monkeypatch.setattr(agent_friday.Agent.default, "llm_node", fake)
    agent = object.__new__(agent_friday.FridayAgent)
    ctx = lk_llm.ChatContext()
    ctx.add_message(role="system", content="persona")
    ctx.add_message(role="user", content="How's the robot arm?")
    ctx.add_message(role="assistant", content="The systems are humming, sir.")
    ctx.add_message(role="user", content=line)

    async def drain():
        return [c async for c in agent.llm_node(ctx, list(tools), None)]

    asyncio.run(drain())
    return seen


def test_small_talk_goes_without_tools_or_the_earlier_conversation(monkeypatch):
    seen = _tools_seen(monkeypatch, "Sup Jarvis.")
    assert seen["tools"] == []
    assert seen["settings"].tool_choice == "none"     # Gemini rejects "auto" with no tools
    assert seen["texts"][0] == "persona"
    assert len(seen["texts"]) == 2 and seen["texts"][-1].endswith("Sup Jarvis.")
    assert "The systems are humming, sir." not in seen["texts"]


def test_requests_keep_their_tools_and_the_conversation(monkeypatch):
    seen = _tools_seen(monkeypatch, "Open Spotify.")
    assert seen["tools"] == ["a_tool"]
    assert seen["settings"] is None
    assert "The systems are humming, sir." in seen["texts"]


def test_a_tool_result_is_never_small_talk():
    ctx = _ctx()
    ctx.items.append(lk_llm.FunctionCallOutput(call_id="1", name="t", output="thanks", is_error=False))
    assert not agent_friday._is_small_talk_turn(ctx)
