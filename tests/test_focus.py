"""Focus mode: "focus for 45 minutes" (friday/focus.py, friday/tools/focus.py)."""
import json
import time
import types
from datetime import datetime

import pytest

import agent_friday
from friday import focus
from friday.scheduling import store as schedule_store
from friday.tools import focus as focus_tool


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A session file, a timer store and the side effects, all stand-ins."""
    monkeypatch.setattr(focus, "FOCUS_FILE", tmp_path / "focus.json")
    monkeypatch.setattr(schedule_store, "SCHEDULED_STORE_PATH", tmp_path / "scheduled.json")
    calls = {"closed": [], "paused": 0, "lofi": 0}
    monkeypatch.setattr(focus_tool, "close_app",
                        lambda name: (calls["closed"].append(name), f"Closed {name.capitalize()}.")[1])
    monkeypatch.setattr(focus_tool.media_control, "pause_everything",
                        lambda: calls.__setitem__("paused", calls["paused"] + 1) or 1)
    monkeypatch.setattr(focus_tool, "_play_lofi", lambda: calls.__setitem__("lofi", calls["lofi"] + 1))
    counts = {"now": 5}
    monkeypatch.setattr(focus, "notification_count", lambda: counts["now"])
    return types.SimpleNamespace(calls=calls, counts=counts, tmp=tmp_path)


def _scheduled(world):
    return json.loads((world.tmp / "scheduled.json").read_text())


def test_starting_holds_closes_pauses_plays_and_schedules_the_end(world):
    reply = focus_tool.start(45)
    time.sleep(0.05)                                    # lo-fi starts on a thread
    assert reply.startswith("Focus mode until ")
    assert "Discord closed" in reply and "lo-fi on" in reply
    assert world.calls == {"closed": ["discord"], "paused": 1, "lofi": 1}

    record = focus.active()
    assert record["until"] - record["started"] == pytest.approx(45 * 60)
    assert record["closed"] == ["Discord"] and record["notifications_at_start"] == 5

    [timer] = _scheduled(world)                         # it orbits as a moon, then recaps
    assert timer["label"] == focus.TIMER_LABEL and timer["kind"] == "timer"
    assert datetime.fromisoformat(timer["fire_at"]).timestamp() == pytest.approx(record["until"], abs=1)


def test_a_second_start_leaves_the_running_session_alone(world):
    focus_tool.start(45)
    reply = focus_tool.start(10)
    assert reply.startswith("Already focusing")
    assert len(_scheduled(world)) == 1


def test_ending_early_recaps_and_clears_everything(world):
    focus_tool.start(45)
    world.counts["now"] = 8
    reply = focus_tool.end()
    assert "3 notifications came in" in reply and "Discord" in reply
    assert "Do Not Disturb is off" in reply
    assert focus.active() is None and _scheduled(world) == []
    assert focus_tool.end().startswith("There's no focus session")


def test_recap_wording():
    record = {"started": 0, "until": 2700, "closed": [], "notifications_at_start": 2}
    assert focus.recap(record, 2, now=2700).startswith("Focus session over after 45 minutes. No notifications")
    assert "1 notification came in" in focus.recap(record, 3, now=2700)
    assert "notification" not in focus.recap(record, None, now=2700)   # sky feed down: say nothing
    assert "after 10 minutes" in focus.recap(record, 2, now=600)       # ended early


def test_a_long_ended_session_is_no_session(world):
    focus.begin(1, [], None, now=time.time() - 3600)
    assert focus.active() is None


def test_the_end_of_a_session_is_a_recap_not_a_stored_line(world):
    focus_tool.start(30)
    world.counts["now"] = 6
    [timer] = schedule_store.all_items()
    told = agent_friday._scheduled_fire_instructions(timer)
    assert "focus session just ended" in told and "1 notification came in" in told
    assert focus.active() is None                       # and the session is over


def test_other_timers_still_say_their_line(world):
    item = types.SimpleNamespace(label="pasta", kind="timer", message="Your pasta timer is up, sir.")
    assert "'Your pasta timer is up, sir.'" in agent_friday._scheduled_fire_instructions(item)
