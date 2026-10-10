"""Cross-OS handoff — leave a task on one OS, pick it up on the other.

This laptop dual-boots, and JARVIS runs on both sides. A request that only
makes sense on the other OS ("FRC mode" needs the Windows-only Driver Station)
is written to a queue, the machine reboots, and the JARVIS that starts there
finds the task, runs it, and marks it done.

Where the queue lives: the two OSes cannot see each other's disks (Windows is
BitLocker-encrypted; Windows cannot read ext4), so there is no folder both can
open. The default queue is therefore a private Google Calendar named
"JARVIS Handoff" — it rides on the Calendar sign-in JARVIS already has on both
sides, so there is nothing new to log into. Each task is one event; its state
is kept in the event's private properties.

`FRIDAY_HANDOFF_DIR` swaps in a plain folder queue instead (tests, or a shared
partition if one ever exists).

Deliberately free of any MCP/LiveKit import: the launcher loads this module.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CALENDAR_NAME = "JARVIS Handoff"
# A task nobody picked up in this long is stale — "FRC mode" from last week
# must not fire on an unrelated boot.
MAX_AGE = timedelta(hours=12)

_ROOT = Path(__file__).resolve().parents[1]
_TOKEN_FILE = _ROOT / "token.json"
_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def current_os() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def other_os() -> str:
    return "linux" if current_os() == "windows" else "windows"


@dataclass
class HandoffTask:
    request: str                       # what to do, as you'd say it to JARVIS
    target_os: str                     # "windows" | "linux" | "any"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_on: str = field(default_factory=lambda: current_os())
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "pending"            # pending | done | failed | expired
    result: str = ""

    def is_for(self, os_name: str) -> bool:
        return self.target_os in (os_name, "any")

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            return now - datetime.fromisoformat(self.created_at) > MAX_AGE
        except ValueError:
            return True


# ---------------------------------------------------------------------------
# Folder queue
# ---------------------------------------------------------------------------

class FolderQueue:
    """One JSON file per task under <dir>/pending, moved to <dir>/done."""

    def __init__(self, directory: Path):
        self._pending = Path(directory) / "pending"
        self._done = Path(directory) / "done"

    def add(self, task: HandoffTask) -> HandoffTask:
        self._pending.mkdir(parents=True, exist_ok=True)
        (self._pending / f"{task.id}.json").write_text(
            json.dumps(asdict(task), indent=2), encoding="utf-8")
        return task

    def pending(self) -> list[HandoffTask]:
        tasks = []
        for path in sorted(self._pending.glob("*.json")) if self._pending.is_dir() else []:
            try:
                tasks.append(HandoffTask(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return tasks

    def finish(self, task: HandoffTask, status: str, result: str = "") -> None:
        task.status, task.result = status, result
        self._done.mkdir(parents=True, exist_ok=True)
        (self._done / f"{task.id}.json").write_text(
            json.dumps(asdict(task), indent=2), encoding="utf-8")
        (self._pending / f"{task.id}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Google Calendar queue
# ---------------------------------------------------------------------------

class CalendarQueue:
    """Tasks as events on a dedicated calendar in the user's Google account."""

    def __init__(self):
        self._service = None
        self._calendar_id = None

    def _api(self):
        if self._service is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            if not _TOKEN_FILE.exists():
                raise RuntimeError("Google sign-in (token.json) is missing")
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
            if not creds.valid:
                creds.refresh(Request())
                _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            self._service = build("calendar", "v3", credentials=creds,
                                  cache_discovery=False)
        return self._service

    def _calendar(self, *, create: bool) -> str | None:
        if self._calendar_id:
            return self._calendar_id
        api = self._api()
        page = None
        while True:
            listing = api.calendarList().list(pageToken=page).execute()
            for entry in listing.get("items", []):
                if entry.get("summary") == CALENDAR_NAME:
                    self._calendar_id = entry["id"]
                    return self._calendar_id
            page = listing.get("nextPageToken")
            if not page:
                break
        if not create:
            return None
        created = api.calendars().insert(body={
            "summary": CALENDAR_NAME,
            "description": "Tasks JARVIS leaves for itself on the other OS. "
                           "Safe to delete; it is recreated when needed.",
        }).execute()
        self._calendar_id = created["id"]
        return self._calendar_id

    def add(self, task: HandoffTask) -> HandoffTask:
        calendar = self._calendar(create=True)
        today = datetime.now().date()
        self._api().events().insert(calendarId=calendar, body={
            "summary": f"[{task.target_os}] {task.request}",
            "start": {"date": today.isoformat()},
            "end": {"date": (today + timedelta(days=1)).isoformat()},
            "transparency": "transparent",
            "reminders": {"useDefault": False},
            "extendedProperties": {"private": {
                "jarvis_task": json.dumps(asdict(task)),
                "jarvis_status": "pending",
            }},
        }).execute()
        return task

    def _events(self) -> list[dict]:
        calendar = self._calendar(create=False)
        if calendar is None:
            return []     # never used on this account: nothing can be pending
        return self._api().events().list(
            calendarId=calendar, privateExtendedProperty="jarvis_status=pending",
            singleEvents=True, maxResults=50,
        ).execute().get("items", [])

    def pending(self) -> list[HandoffTask]:
        tasks = []
        for event in self._events():
            raw = event.get("extendedProperties", {}).get("private", {}).get("jarvis_task")
            try:
                task = HandoffTask(**json.loads(raw))
            except (TypeError, ValueError):
                continue
            task._event_id = event["id"]          # type: ignore[attr-defined]
            tasks.append(task)
        return tasks

    def finish(self, task: HandoffTask, status: str, result: str = "") -> None:
        task.status, task.result = status, result
        event_id = getattr(task, "_event_id", None)
        if event_id is None:
            return
        self._api().events().patch(
            calendarId=self._calendar(create=False), eventId=event_id, body={
                "description": f"{status} on {current_os()}: {result}"[:900],
                "extendedProperties": {"private": {"jarvis_status": status}},
            }).execute()


def get_queue():
    directory = os.environ.get("FRIDAY_HANDOFF_DIR")
    return FolderQueue(Path(directory)) if directory else CalendarQueue()


# ---------------------------------------------------------------------------
# What callers use
# ---------------------------------------------------------------------------

def leave_task(request: str, target_os: str | None = None, queue=None) -> HandoffTask:
    """Queue `request` for the other OS (or the one named)."""
    target = (target_os or other_os()).lower()
    if target not in ("windows", "linux", "any"):
        raise ValueError(f"unknown OS: {target_os!r}")
    return (queue or get_queue()).add(HandoffTask(request=request.strip(), target_os=target))


def claim_pending(queue=None, *, retries: int = 3, delay: float = 4.0) -> list[HandoffTask]:
    """Tasks waiting for THIS OS, oldest first. Stale ones are retired, not run.

    Retries because this runs at boot, when the network may not be up yet.
    """
    queue = queue or get_queue()
    last_error = None
    for attempt in range(retries):
        try:
            mine = [t for t in queue.pending() if t.is_for(current_os())]
            break
        except Exception as e:      # offline, token trouble, API hiccup
            last_error = e
            if attempt + 1 < retries:
                time.sleep(delay)
    else:
        raise RuntimeError(f"handoff queue unreachable: {last_error}")

    fresh = []
    for task in sorted(mine, key=lambda t: t.created_at):
        if task.is_stale():
            queue.finish(task, "expired", "not picked up in time")
        else:
            fresh.append(task)
    return fresh
