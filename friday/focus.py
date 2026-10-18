"""Focus sessions: notifications held, distractions closed, lo-fi on, a recap at the end.

runtime/focus.json is the one source of truth while a session runs:
  - start_focus / end_focus (friday/tools/focus.py) write and remove it,
  - the sky feed (friday/sky_feed.py) holds Do Not Disturb while it's there and
    tells the wallpaper to dim and keep its comets in,
  - the session's end is a scheduled timer labelled TIMER_LABEL, so it orbits
    the black hole as a moon; when it fires the agent speaks recap() instead of
    a plain "time's up", and finish() ends the session.

Deliberately free of any MCP/LiveKit import: the agent, the tool server and
the sky feed all load it.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

FOCUS_FILE = Path(__file__).resolve().parents[1] / "runtime" / "focus.json"
TIMER_LABEL = "focus"
# A session still counts this long past its end, while its recap is on the way.
# Past that a leftover file (JARVIS was off when it ended) is ignored.
GRACE = 120.0
_SKY = "http://127.0.0.1:47813/sky?after=-1"


def load(path: Path | None = None) -> dict | None:
    try:
        record = json.loads((path or FOCUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and "until" in record else None


def active(path: Path | None = None, now: float | None = None) -> dict | None:
    """The running session, if there is one."""
    record = load(path)
    now = time.time() if now is None else now
    return record if record and now < record["until"] + GRACE else None


def begin(minutes: int, closed: list[str], notifications: int | None,
          path: Path | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    path = path or FOCUS_FILE
    record = {"started": now, "until": now + minutes * 60, "minutes": minutes,
              "closed": closed, "notifications_at_start": notifications}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def finish(path: Path | None = None) -> dict | None:
    """End the session; its record, if there was one."""
    record = load(path)
    (path or FOCUS_FILE).unlink(missing_ok=True)
    return record


def notification_count() -> int | None:
    """The sky feed's running count of desktop notifications; None if it isn't up."""
    try:
        with urllib.request.urlopen(_SKY, timeout=2) as reply:
            return int(json.loads(reply.read())["notifications"])
    except Exception:
        return None


def recap(record: dict, notifications_now: int | None, now: float | None = None) -> str:
    """The facts of a session that just ended, for JARVIS to put in his own words."""
    now = time.time() if now is None else now
    minutes = max(1, round((min(now, record["until"]) - record["started"]) / 60))
    facts = [f"Focus session over after {minutes} minute{'s' if minutes != 1 else ''}."]
    start = record.get("notifications_at_start")
    if start is not None and notifications_now is not None:
        held = max(0, notifications_now - start)
        facts.append("No notifications came in." if held == 0 else
                     f"{held} notification{'s' if held != 1 else ''} came in meanwhile; "
                     "they're in the notification history.")
    if record.get("closed"):
        facts.append(f"{' and '.join(record['closed'])} had been closed for it.")
    facts.append("Do Not Disturb is off again.")
    return " ".join(facts)
