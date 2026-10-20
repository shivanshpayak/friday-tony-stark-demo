"""Alarm times and repeat days — parsing what the model passes, and when next.

Pure functions, no I/O: the tools in friday/tools/scheduler.py parse with
these, and the tick loop in service.py asks next_occurrence() where a repeating
alarm goes after it fires.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, time, timedelta

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

WEEKDAYS = (0, 1, 2, 3, 4)
WEEKENDS = (5, 6)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)

_GROUPS = {
    "weekdays": WEEKDAYS, "weekday": WEEKDAYS,
    "weekends": WEEKENDS, "weekend": WEEKENDS,
    "daily": EVERY_DAY, "everyday": EVERY_DAY,
}
_ONCE = {"", "once", "none", "never", "no", "one time", "just once"}
_FILLER = {"and", "on", "every", "each"}
_RANGE = {"-", "to", "through", "thru", "until", "till"}

# "mon", "tues", "thursdays", "wednesday" → its weekday number.
_DAY_PATTERN = re.compile(
    r"^(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)"
    r"(day|nesday|sday|rsday|urday)?s?$"
)
_DAY_INDEX = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3,
              "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def _day(word: str) -> int | None:
    m = _DAY_PATTERN.match(word)
    return _DAY_INDEX[m.group(1)] if m else None


def parse_days(text: str) -> tuple[int, ...]:
    """Weekday numbers (0=Monday) an alarm repeats on; () for a one-time alarm.

    Takes "weekdays", "daily", "weekends", day names in any common spelling
    ("mon wed fri", "Tuesdays and Thursdays"), and ranges ("mon-fri",
    "Monday to Thursday"). Raises ValueError on anything else.
    """
    s = re.sub(r"[,;/&+]", " ", (text or "").lower())
    s = " ".join(s.replace("-", " - ").replace("school days", "weekdays").split())
    if s in _ONCE:
        return ()
    if s in ("every day", "every single day"):
        return EVERY_DAY
    days: list[int] = []
    pending_range = False
    for word in (w for w in s.split() if w not in _FILLER):
        if word in _RANGE:
            if not days:
                raise ValueError(f"no day before {word!r}")
            pending_range = True
            continue
        if word in _GROUPS:
            days.extend(_GROUPS[word])
            continue
        day = _day(word)
        if day is None:
            raise ValueError(f"not a day: {word!r}")
        if pending_range:
            start = days[-1]
            days.extend((start + i) % 7 for i in range(1, (day - start) % 7 + 1))
            pending_range = False
        else:
            days.append(day)
    if pending_range:
        raise ValueError("a range with no end day")
    return tuple(sorted(set(days)))


def describe_days(days: Sequence[int]) -> str:
    """'on weekdays', 'every day', 'on Mondays and Thursdays'; '' for none."""
    days = tuple(sorted(set(days)))
    if not days:
        return ""
    if days == EVERY_DAY:
        return "every day"
    if days == WEEKDAYS:
        return "on weekdays"
    if days == WEEKENDS:
        return "on weekends"
    names = [f"{DAY_NAMES[d]}s" for d in days]
    if len(names) == 1:
        return f"on {names[0]}"
    return f"on {', '.join(names[:-1])} and {names[-1]}"


_CLOCK = re.compile(
    r"^(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?(?::\d{2})?\s*(?P<ampm>[ap])?\.?\s*(?:m\.?)?$"
)


def parse_clock(text: str) -> tuple[time, datetime | None]:
    """The time of day in `text`, and the full date-time if it named a date.

    Takes "09:12", "9:12", "21:05", "9:12 PM", "7am", "noon", "midnight", or a
    local ISO timestamp ("2026-10-21T07:00:00"). Raises ValueError otherwise.
    """
    raw = (text or "").strip()
    s = raw.lower()
    if s == "noon":
        return time(12, 0), None
    if s == "midnight":
        return time(0, 0), None
    if "t" in s or "-" in s:
        try:
            dt = datetime.fromisoformat(raw).replace(second=0, microsecond=0, tzinfo=None)
        except ValueError:
            raise ValueError(f"not a time: {raw!r}") from None
        return dt.time(), dt
    m = _CLOCK.match(s)
    if not m:
        raise ValueError(f"not a time: {raw!r}")
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    ampm = m.group("ampm")
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"not a time: {raw!r}")
        hour = hour % 12 + (12 if ampm == "p" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"not a time: {raw!r}")
    return time(hour, minute), None


def next_occurrence(at: time, days: Sequence[int], after: datetime) -> datetime:
    """The first time `at` comes round strictly after `after`, on one of
    `days` (any day when empty)."""
    for offset in range(8):
        day = after.date() + timedelta(days=offset)
        candidate = datetime.combine(day, at)
        if candidate > after and (not days or day.weekday() in days):
            return candidate
    raise ValueError(f"no weekday in {list(days)!r}")


def clock_text(t: time) -> str:
    """'9:12 AM' — as spoken and listed."""
    return datetime.combine(datetime.min.date(), t).strftime("%I:%M %p").lstrip("0")
