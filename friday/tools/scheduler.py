"""Timer, reminder and alarm MCP tools — backed by friday/scheduling/."""
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

from friday.scheduling import ScheduledItem, add, all_items, find, remove, update
from friday.scheduling.recurrence import (
    clock_text,
    describe_days,
    next_occurrence,
    parse_clock,
    parse_days,
)


def _format_when(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        return iso_ts
    today = datetime.now().date()
    if dt.date() == today:
        return dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == today + timedelta(days=1):
        return f"tomorrow at {dt.strftime('%I:%M %p').lstrip('0')}"
    return dt.strftime("%a %b %d at %I:%M %p").replace(" 0", " ")


def _duration(total_seconds: int) -> str:
    """'30 seconds', '10 minutes', '1 minute 30 seconds'."""
    minutes, seconds = divmod(total_seconds, 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " ".join(parts)


def _duration_adjective(total_seconds: int) -> str:
    """'30-second', '10-minute', '1-minute 30-second' — as in "your 10-minute timer"."""
    minutes, seconds = divmod(total_seconds, 60)
    parts = []
    if minutes:
        parts.append(f"{minutes}-minute")
    if seconds:
        parts.append(f"{seconds}-second")
    return " ".join(parts)


def _alarm_name(label: str) -> str:
    """'First period alarm', or 'Alarm' without a label."""
    return f"{label[:1].upper()}{label[1:]} alarm" if label else "Alarm"


def _alarm_when(item: ScheduledItem) -> str:
    """'9:12 AM on weekdays', or for a one-time alarm 'tomorrow at 7:00 AM'."""
    if item.repeat:
        return f"{clock_text(datetime.fromisoformat(item.fire_at).time())} {describe_days(item.repeat)}"
    return _format_when(item.fire_at)


def _alarms_named(label_or_id: str) -> list[ScheduledItem]:
    """The alarm with this label or ID — or every alarm, for "all"."""
    if label_or_id.strip().lower() in ("all", "every", "everything", "all alarms"):
        return [i for i in all_items() if i.kind == "alarm"]
    match = find(label_or_id)
    return [match] if match and match.kind == "alarm" else []


def register(mcp: FastMCP):

    @mcp.tool(name="set_timer")
    def set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> str:
        """Set a countdown timer that announces when it expires.
        Use when the user says "set a timer for X minutes", "X seconds", or
        "remind me in X minutes". Give the duration as `minutes`, `seconds`, or
        both; fractions are fine. Optional `label` distinguishes multiple
        concurrent timers (e.g. "pasta", "laundry"). Examples:
          set_timer(10) → "Timer set for 10 minutes."
          set_timer(seconds=30) → "Timer set for 30 seconds."
          set_timer(1, 30) → "Timer set for 1 minute 30 seconds."
          set_timer(8, label="pasta") → "Pasta timer set for 8 minutes."
        """
        # Was `minutes: int`: "thirty seconds" came in as minutes=0.5 and failed
        # validation, so nothing under a minute could be set.
        total = round(minutes * 60 + seconds)
        if total <= 0:
            return "Timer duration must be positive."
        fire_at = (datetime.now() + timedelta(seconds=total)).isoformat()
        if label:
            message = f"Your {label} timer is up, sir."
            ack_label = f"{label.capitalize()} timer"
        else:
            message = f"Your {_duration_adjective(total)} timer is up, sir."
            ack_label = "Timer"
        item = ScheduledItem(
            kind="timer",
            fire_at=fire_at,
            message=message,
            label=label,
            created_at=datetime.now().isoformat(),
        )
        add(item)
        return f"{ack_label} set for {_duration(total)}."

    @mcp.tool(name="set_reminder")
    def set_reminder(when_iso: str, message: str, label: str = "") -> str:
        """Schedule a reminder that Jarvis will speak once at the given time.
        For something at the same time every day or on certain weekdays, use
        set_alarm instead. `when_iso` MUST be a local-time ISO 8601 timestamp (e.g.
        '2026-04-26T15:00:00'). Convert natural-language times like
        "3pm" or "tomorrow at 7am" into ISO yourself based on the current
        time before calling. `message` is the reminder content. Optional
        `label` allows targeted cancellation. Examples:
          set_reminder("2026-04-26T15:00:00", "call John")
          set_reminder("2026-04-27T07:00:00", "take out the trash", label="trash")
        """
        try:
            when_dt = datetime.fromisoformat(when_iso)
        except Exception:
            return f"Invalid timestamp: {when_iso!r}. Use ISO 8601 like '2026-04-26T15:00:00'."
        if when_dt <= datetime.now():
            return "That time is already in the past."
        item = ScheduledItem(
            kind="reminder",
            fire_at=when_iso,
            message=f"Reminder, sir: {message}",
            label=label,
            created_at=datetime.now().isoformat(),
        )
        add(item)
        return f"Reminder set for {_format_when(when_iso)}: {message}."

    @mcp.tool(name="set_alarm")
    def set_alarm(time: str, repeat: str = "", label: str = "") -> str:
        """Set an alarm for a clock time. It rings until he says "stop" (or
        "snooze", for ten more minutes). Use for "wake me up at 7", "set an
        alarm for 6:30", and anything at the same time on certain days —
        "every weekday at 9:12, when first period ends". For "in X minutes"
        use set_timer; for a one-off note to be spoken, set_reminder.
        `time`: 24-hour local "HH:MM" ("07:00", "13:45"). For a one-time
        alarm on a later day, a local ISO timestamp ("2026-10-23T07:00:00").
        `repeat`: "" rings once, the next time that clock time comes round;
        otherwise "weekdays", "daily", "weekends", or day names ("mon wed fri").
        `label`: a short name, said when it rings and used to change or cancel
        it ("first period", "gym"). An existing alarm's label moves that alarm.
        Examples:
          set_alarm("07:00") → "Alarm set for tomorrow at 7:00 AM."
          set_alarm("09:12", "weekdays", "first period") →
            "First period alarm set for 9:12 AM on weekdays."
        """
        try:
            at, pinned = parse_clock(time)
            days = parse_days(repeat)
        except ValueError as e:
            return (f"Couldn't set that alarm: {e}. Give `time` as 24-hour HH:MM "
                    "and `repeat` as weekdays, daily, weekends, or day names.")
        now = datetime.now()
        if pinned is not None and not days:
            if pinned <= now:
                return "That time is already in the past."
            fire_at = pinned
        else:
            fire_at = next_occurrence(at, days, now)
        label = label.strip()
        existing = find(label) if label else None
        item = ScheduledItem(
            kind="alarm",
            fire_at=fire_at.isoformat(),
            message=f"Your {label} alarm, sir." if label else "Your alarm, sir.",
            label=label,
            created_at=now.isoformat(),
            repeat=list(days),
        )
        if existing is not None and existing.kind == "alarm":
            update(item.model_copy(update={"id": existing.id}))
            return f"{_alarm_name(label)} moved to {_alarm_when(item)}."
        add(item)
        return f"{_alarm_name(label)} set for {_alarm_when(item)}."

    @mcp.tool(name="switch_alarm")
    def switch_alarm(label_or_id: str, on: bool) -> str:
        """Turn a saved alarm off, or back on, without deleting it — "turn off
        my first period alarm", "turn my alarms back on". `label_or_id` is its
        label or 8-character ID, or "all" for every alarm (e.g. off for a
        school break). To delete one for good, use cancel_scheduled."""
        targets = _alarms_named(label_or_id)
        if not targets:
            return f"No alarm matches {label_or_id!r}."
        now = datetime.now()
        for item in targets:
            changes: dict = {"enabled": on}
            due = datetime.fromisoformat(item.fire_at)
            if on and due <= now:
                # Off through its time: it rings at the next one, not at once.
                changes["fire_at"] = next_occurrence(due.time(), item.repeat, now).isoformat()
            update(item.model_copy(update=changes))
        state = "on" if on else "off"
        if len(targets) > 1:
            return f"All {len(targets)} alarms {state}."
        return f"{_alarm_name(targets[0].label)} {state}."

    @mcp.tool(name="list_scheduled")
    def list_scheduled() -> str:
        """List all pending timers, reminders and alarms. Use when the user asks
        "what's scheduled", "do I have any timers", "what alarms do I have"."""
        items = sorted(all_items(), key=lambda i: i.fire_at)
        if not items:
            return "Nothing scheduled, sir."
        lines = []
        for i in items:
            handle = i.label or i.id
            if i.kind == "alarm":
                off = "" if i.enabled else " (switched off)"
                lines.append(f"- alarm ({handle}) at {_alarm_when(i)}{off}")
                continue
            when = _format_when(i.fire_at)
            preview = i.message.replace("Reminder, sir: ", "").replace(", sir.", "")
            lines.append(f"- {i.kind} ({handle}) at {when}: {preview}")
        return "\n".join(lines)

    @mcp.tool(name="cancel_scheduled")
    def cancel_scheduled(label_or_id: str) -> str:
        """Cancel a pending timer, reminder or alarm by its label (e.g. "pasta")
        or its 8-character ID. Use when the user says "cancel my X timer",
        "cancel that reminder", or "delete my X alarm"."""
        match = find(label_or_id)
        if not match:
            return f"No scheduled item matches {label_or_id!r}."
        remove(match.id)
        handle = match.label or match.id
        return f"Cancelled {match.kind} ({handle})."
