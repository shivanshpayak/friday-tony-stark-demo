"""Timer + reminder MCP tools — backed by friday/scheduling/."""
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

from friday.scheduling import ScheduledItem, add, all_items, find, remove


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


def register(mcp: FastMCP):

    @mcp.tool(name="set_timer")
    def set_timer(minutes: int, label: str = "") -> str:
        """Set a countdown timer that announces when it expires.
        Use when the user says "set a timer for X minutes" or "remind me in X
        minutes". Optional `label` distinguishes multiple concurrent timers
        (e.g. "pasta", "laundry"). Examples:
          set_timer(10) → "Timer set for 10 minutes."
          set_timer(8, "pasta") → "Pasta timer set for 8 minutes."
        """
        if minutes <= 0:
            return "Timer duration must be positive."
        fire_at = (datetime.now() + timedelta(minutes=minutes)).isoformat()
        if label:
            message = f"Your {label} timer is up, sir."
            ack_label = f"{label.capitalize()} timer"
        else:
            message = f"Your {minutes}-minute timer is up, sir."
            ack_label = "Timer"
        item = ScheduledItem(
            kind="timer",
            fire_at=fire_at,
            message=message,
            label=label,
            created_at=datetime.now().isoformat(),
        )
        add(item)
        return f"{ack_label} set for {minutes} minute{'s' if minutes != 1 else ''}."

    @mcp.tool(name="set_reminder")
    def set_reminder(when_iso: str, message: str, label: str = "") -> str:
        """Schedule a reminder that Jarvis will speak at the given time.
        `when_iso` MUST be a local-time ISO 8601 timestamp (e.g.
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

    @mcp.tool(name="list_scheduled")
    def list_scheduled() -> str:
        """List all pending timers and reminders. Use when the user asks
        "what's scheduled", "do I have any timers", "what reminders do I have"."""
        items = sorted(all_items(), key=lambda i: i.fire_at)
        if not items:
            return "Nothing scheduled, sir."
        lines = []
        for i in items:
            handle = i.label or i.id
            when = _format_when(i.fire_at)
            preview = i.message.replace("Reminder, sir: ", "").replace(", sir.", "")
            lines.append(f"- {i.kind} ({handle}) at {when}: {preview}")
        return "\n".join(lines)

    @mcp.tool(name="cancel_scheduled")
    def cancel_scheduled(label_or_id: str) -> str:
        """Cancel a pending timer or reminder by its label (e.g. "pasta")
        or its 8-character ID. Use when the user says "cancel my X timer"
        or "cancel that reminder"."""
        match = find(label_or_id)
        if not match:
            return f"No scheduled item matches {label_or_id!r}."
        remove(match.id)
        handle = match.label or match.id
        return f"Cancelled {match.kind} ({handle})."
