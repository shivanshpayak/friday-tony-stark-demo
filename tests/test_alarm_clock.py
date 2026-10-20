"""Clock alarms (set_alarm): a time of day, repeating on chosen weekdays, with
an on/off switch — the Windows Clock alarm, by voice. Asked for to go off when
first and fifth period end.

The tools run through a real FastMCP so arguments get the agent's validation,
against a store in tmp_path. The scheduler's tick is collect_due(), driven
with a fixed `now`. 2026-10-20 is a Tuesday.
"""
import asyncio
from datetime import datetime, time

import pytest
from mcp.server.fastmcp import FastMCP

import friday.alarm as alarm
import friday.scheduling.store as store
import friday.tools.scheduler as scheduler
from friday.scheduling import ScheduledItem
from friday.scheduling.recurrence import (
    describe_days,
    next_occurrence,
    parse_clock,
    parse_days,
)
from friday.scheduling.service import collect_due

TUESDAY = datetime(2026, 10, 20, 8, 0)


@pytest.fixture(autouse=True)
def scratch_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "SCHEDULED_STORE_PATH", tmp_path / "scheduled.json")


@pytest.fixture
def call():
    mcp = FastMCP("test")
    scheduler.register(mcp)

    def _call(tool, **arguments):
        result = asyncio.run(mcp.call_tool(tool, arguments))
        blocks = result[0] if isinstance(result, tuple) else result
        return blocks[0].text

    return _call


def _alarm(fire_at: datetime, repeat=(), label="", enabled=True) -> ScheduledItem:
    item = ScheduledItem(kind="alarm", fire_at=fire_at.isoformat(),
                         message=f"Your {label} alarm, sir.", label=label,
                         created_at=TUESDAY.isoformat(), repeat=list(repeat),
                         enabled=enabled)
    store.add(item)
    return item


# --- reading what the model passes ----------------------------------------------

@pytest.mark.parametrize("text, days", [
    ("weekdays", (0, 1, 2, 3, 4)),
    ("every weekday", (0, 1, 2, 3, 4)),
    ("mon-fri", (0, 1, 2, 3, 4)),
    ("Monday to Thursday", (0, 1, 2, 3)),
    ("Mondays, Wednesdays & Fridays", (0, 2, 4)),
    ("tues and thurs", (1, 3)),
    ("fri - mon", (0, 4, 5, 6)),       # a range across the weekend
    ("daily", (0, 1, 2, 3, 4, 5, 6)),
    ("every day", (0, 1, 2, 3, 4, 5, 6)),
    ("weekends", (5, 6)),
    ("", ()),
    ("once", ()),
])
def test_repeat_days(text, days):
    assert parse_days(text) == days


@pytest.mark.parametrize("text", ["funday", "mon to", "tomorrow"])
def test_unknown_days_are_refused(text):
    with pytest.raises(ValueError):
        parse_days(text)


def test_days_read_back():
    assert describe_days((0, 1, 2, 3, 4)) == "on weekdays"
    assert describe_days((1, 3)) == "on Tuesdays and Thursdays"
    assert describe_days((0, 2, 4)) == "on Mondays, Wednesdays and Fridays"
    assert describe_days(()) == ""


@pytest.mark.parametrize("text, at", [
    ("09:12", time(9, 12)), ("9:12", time(9, 12)), ("13:45", time(13, 45)),
    ("1:45 PM", time(13, 45)), ("7am", time(7, 0)), ("12 a.m.", time(0, 0)),
    ("noon", time(12, 0)),
])
def test_clock_times(text, at):
    assert parse_clock(text) == (at, None)


def test_an_iso_timestamp_pins_the_date():
    assert parse_clock("2026-10-23T07:00:00") == (time(7, 0), datetime(2026, 10, 23, 7, 0))


@pytest.mark.parametrize("text", ["25:00", "13pm", "soon", ""])
def test_bad_times_are_refused(text):
    with pytest.raises(ValueError):
        parse_clock(text)


# --- when it rings next ------------------------------------------------------------

def test_later_today_when_the_time_is_still_to_come():
    assert next_occurrence(time(9, 12), (), TUESDAY) == datetime(2026, 10, 20, 9, 12)


def test_tomorrow_when_it_has_passed():
    assert next_occurrence(time(7, 0), (), TUESDAY) == datetime(2026, 10, 21, 7, 0)


def test_friday_afternoon_weekday_alarm_skips_to_monday():
    friday_3pm = datetime(2026, 10, 23, 15, 0)
    assert next_occurrence(time(9, 12), (0, 1, 2, 3, 4), friday_3pm) == datetime(2026, 10, 26, 9, 12)


def test_a_weekly_alarm_just_rung_comes_back_in_a_week():
    rung = datetime(2026, 10, 20, 9, 12)
    assert next_occurrence(time(9, 12), (1,), rung) == datetime(2026, 10, 27, 9, 12)


# --- the tools -------------------------------------------------------------------------

def test_period_end_alarms(call):
    assert call("set_alarm", time="09:12", repeat="weekdays", label="first period") \
        == "First period alarm set for 9:12 AM on weekdays."
    assert call("set_alarm", time="13:45", repeat="weekdays", label="fifth period") \
        == "Fifth period alarm set for 1:45 PM on weekdays."
    items = {i.label: i for i in store.all_items()}
    assert items["first period"].repeat == [0, 1, 2, 3, 4]
    assert items["first period"].message == "Your first period alarm, sir."
    assert datetime.fromisoformat(items["fifth period"].fire_at).time() == time(13, 45)


def test_one_time_alarm(call):
    reply = call("set_alarm", time="07:00")
    item, = store.all_items()
    assert reply.startswith("Alarm set for ") and "7:00 AM" in reply
    assert item.repeat == [] and item.message == "Your alarm, sir."
    assert datetime.fromisoformat(item.fire_at) > datetime.now()


def test_setting_a_label_again_moves_that_alarm(call):
    call("set_alarm", time="09:12", repeat="weekdays", label="first period")
    reply = call("set_alarm", time="09:15", repeat="weekdays", label="First Period")
    item, = store.all_items()
    assert reply == "First Period alarm moved to 9:15 AM on weekdays."
    assert datetime.fromisoformat(item.fire_at).time() == time(9, 15)


def test_a_bad_time_gets_a_usable_error(call):
    reply = call("set_alarm", time="after lunch")
    assert reply.startswith("Couldn't set that alarm")
    assert store.all_items() == []


def test_a_past_pinned_time_is_refused(call):
    assert call("set_alarm", time="2020-01-01T07:00:00") == "That time is already in the past."


def test_switch_off_and_back_on(call):
    call("set_alarm", time="09:12", repeat="weekdays", label="first period")
    assert call("switch_alarm", label_or_id="first period", on=False) == "First period alarm off."
    assert not store.all_items()[0].enabled
    assert "(switched off)" in call("list_scheduled")
    assert call("switch_alarm", label_or_id="first period", on=True) == "First period alarm on."
    assert store.all_items()[0].enabled


def test_switched_back_on_after_its_time_it_waits_for_the_next(call):
    stale = _alarm(datetime(2026, 1, 5, 9, 12), repeat=(0, 1, 2, 3, 4),
                   label="first period", enabled=False)
    call("switch_alarm", label_or_id=stale.id, on=True)
    item, = store.all_items()
    assert datetime.fromisoformat(item.fire_at) > datetime.now()
    assert datetime.fromisoformat(item.fire_at).time() == time(9, 12)


def test_all_alarms_off_for_a_break(call):
    call("set_alarm", time="09:12", repeat="weekdays", label="first period")
    call("set_alarm", time="13:45", repeat="weekdays", label="fifth period")
    call("set_timer", minutes=10, label="pasta")
    assert call("switch_alarm", label_or_id="all", on=False) == "All 2 alarms off."
    assert {i.label: i.enabled for i in store.all_items()} == {
        "first period": False, "fifth period": False, "pasta": True}


def test_switch_does_not_touch_a_timer(call):
    call("set_timer", minutes=10, label="pasta")
    assert call("switch_alarm", label_or_id="pasta", on=False) == "No alarm matches 'pasta'."


def test_listed_with_its_days(call):
    call("set_alarm", time="09:12", repeat="weekdays", label="first period")
    assert call("list_scheduled") == "- alarm (first period) at 9:12 AM on weekdays"


def test_cancel_deletes_it(call):
    call("set_alarm", time="09:12", repeat="weekdays", label="first period")
    assert call("cancel_scheduled", label_or_id="first period") == "Cancelled alarm (first period)."
    assert store.all_items() == []


# --- the scheduler's tick ------------------------------------------------------------

def test_a_repeating_alarm_fires_and_goes_back_on_for_its_next_day():
    _alarm(datetime(2026, 10, 20, 9, 12), repeat=(0, 1, 2, 3, 4), label="first period")
    fired = collect_due(datetime(2026, 10, 20, 9, 12, 1))
    assert [i.label for i in fired] == ["first period"]
    item, = store.all_items()
    assert item.fire_at == "2026-10-21T09:12:00"


def test_a_one_time_alarm_fires_once_and_is_gone():
    _alarm(datetime(2026, 10, 20, 7, 0))
    assert len(collect_due(datetime(2026, 10, 20, 7, 0, 1))) == 1
    assert store.all_items() == []


def test_nothing_fires_early():
    _alarm(datetime(2026, 10, 20, 9, 12), repeat=(1,))
    assert collect_due(datetime(2026, 10, 20, 9, 11, 59)) == []
    assert len(store.all_items()) == 1


def test_an_alarm_missed_while_asleep_is_skipped_but_still_repeats():
    """Laptop opened at 5 PM: no "first period" alarm then, but it rings tomorrow."""
    _alarm(datetime(2026, 10, 20, 9, 12), repeat=(0, 1, 2, 3, 4), label="first period")
    assert collect_due(datetime(2026, 10, 20, 17, 0)) == []
    assert store.all_items()[0].fire_at == "2026-10-21T09:12:00"


def test_a_little_late_still_rings():
    _alarm(datetime(2026, 10, 20, 9, 12), repeat=(1,))
    assert len(collect_due(datetime(2026, 10, 20, 9, 14))) == 1


def test_a_switched_off_alarm_stays_put():
    _alarm(datetime(2026, 10, 20, 9, 12), repeat=(1,), enabled=False)
    assert collect_due(datetime(2026, 10, 20, 9, 13)) == []
    assert store.all_items()[0].fire_at == "2026-10-20T09:12:00"


def test_a_late_timer_still_fires_to_be_said_once():
    store.add(ScheduledItem(kind="timer", fire_at="2026-10-20T07:00:00",
                            message="Your timer is up, sir.", created_at="2026-10-20T06:50:00"))
    assert len(collect_due(TUESDAY)) == 1


# --- ringing and snoozing ------------------------------------------------------------

def test_an_alarm_rings():
    assert alarm.should_ring("alarm", "2026-10-20T09:12:00", datetime(2026, 10, 20, 9, 13))


def test_snooze_is_heard_and_wins_over_a_stop():
    assert alarm.stop_phrase_in("snooze") == "snooze"
    assert alarm.stop_phrase_in("[unk] snooze") == "snooze"
    assert alarm.is_snooze(alarm.stop_phrase_in("okay snooze"))
    assert not alarm.is_snooze(alarm.stop_phrase_in("stop"))
    assert "snooze" in alarm.grammar()


def test_snooze_rings_it_again_in_ten_minutes_under_its_own_label():
    _alarm(datetime(2026, 10, 21, 9, 12), repeat=(0, 1, 2, 3, 4), label="first period")
    now = datetime(2026, 10, 20, 9, 12, 30)
    at = alarm.snooze({"kind": "alarm", "message": "Your first period alarm, sir.",
                       "label": "first period"}, now)
    assert at == datetime(2026, 10, 20, 9, 22, 30)
    snoozed = next(i for i in store.all_items() if i.label == "snoozed first period")
    assert snoozed.kind == "alarm" and snoozed.repeat == []
    assert snoozed.message == "Your first period alarm, sir."
    # "cancel my first period alarm" still finds the repeating one.
    assert store.find("first period").repeat == [0, 1, 2, 3, 4]
