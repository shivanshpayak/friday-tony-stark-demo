"""set_timer through a real FastMCP, so arguments get the same validation the
agent's calls do. "Thirty seconds" arrived as minutes=0.5 (logs, 2026-10-20)
and failed `minutes: int` validation, so no timer under a minute could be set.
Nothing is written to runtime/scheduled.json — `add` is captured."""
import asyncio
from datetime import datetime, timedelta

import pytest
from mcp.server.fastmcp import FastMCP

import friday.tools.scheduler as scheduler


@pytest.fixture
def timer(monkeypatch):
    added = []
    monkeypatch.setattr(scheduler, "add", added.append)
    mcp = FastMCP("test")
    scheduler.register(mcp)

    def call(**arguments):
        result = asyncio.run(mcp.call_tool("set_timer", arguments))
        blocks = result[0] if isinstance(result, tuple) else result
        return blocks[0].text, added

    return call


def _due_in(item) -> float:
    return (datetime.fromisoformat(item.fire_at) - datetime.now()).total_seconds()


def test_half_a_minute_is_thirty_seconds(timer):
    reply, added = timer(minutes=0.5)
    assert reply == "Timer set for 30 seconds."
    assert added[0].message == "Your 30-second timer is up, sir."
    assert 28 < _due_in(added[0]) <= 30


def test_seconds_on_their_own(timer):
    reply, added = timer(seconds=45)
    assert reply == "Timer set for 45 seconds."
    assert 43 < _due_in(added[0]) <= 45


def test_minutes_and_seconds_together(timer):
    reply, added = timer(minutes=1, seconds=30)
    assert reply == "Timer set for 1 minute 30 seconds."
    assert added[0].message == "Your 1-minute 30-second timer is up, sir."


def test_whole_minutes_read_as_before(timer):
    reply, added = timer(minutes=10)
    assert reply == "Timer set for 10 minutes."
    assert added[0].message == "Your 10-minute timer is up, sir."
    assert timedelta(minutes=10) - timedelta(seconds=2) < timedelta(seconds=_due_in(added[0]))


def test_labelled_timer(timer):
    reply, added = timer(seconds=90, label="pasta")
    assert reply == "Pasta timer set for 1 minute 30 seconds."
    assert added[0].message == "Your pasta timer is up, sir."


def test_no_duration_is_refused(timer):
    reply, added = timer()
    assert reply == "Timer duration must be positive."
    assert added == []
