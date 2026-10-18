"""What's in JARVIS's sky right now, for the wallpaper to draw.

The Event Horizon wallpaper (~/event-horizon) puts JARVIS's business around
its black hole: each running timer is a moon whose trail closes a full circle
as time runs out, each background task (a "do X until Y" loop, a Claude job)
is a satellite, and every desktop notification sends a comet across the sky.
This watches the sources and serves a snapshot as a long poll on loopback:

    GET http://127.0.0.1:47813/sky?after=<seq>
    -> {"seq": 7,
        "timers": [{"id": "ab12cd34", "start": 1760770000.0, "end": 1760770600.0}],
        "tasks": [{"id": "loop_1a2b3c", "status": "running"}],
        "notifications": 3,
        "focus": {"started": 1760770000.0, "until": 1760772700.0}}

The answer comes as soon as the snapshot changes (a notification counts), or
after 25 s. Sources: runtime/scheduled.json (timers, and reminders in their
last 15 minutes), runtime/tasks/active/*.json (unfinished tasks touched in the
last 3 hours, so a record a crash left behind doesn't orbit forever, plus ones
that ended in the last 90 s, with how they ended), and the session bus's Notify calls, counted through `dbus-monitor --profile`, which
reports that a notification was sent and nothing of what it says.

It also runs focus sessions' Do Not Disturb: while runtime/focus.json says a
session is on (friday/focus.py), it holds KDE's notification inhibition, which
KDE lifts by itself if this process goes away, so a crash can't leave it on.

The launcher runs this as a subprocess (friday_overlay._SubprocessOverlay); it
exits when its stdin closes. Read-only, loopback only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from friday import focus

PORT = 47813
_WAIT = 25.0
_RETRY = 2.0
_SCAN = 0.5                     # s between looks at the timer and task files
REMINDER_LEAD = 15 * 60         # a reminder joins the sky this long before it fires
TASK_STALE = 3 * 3600           # an unfinished task untouched this long is a leftover
TASK_ENDED = 90                 # a finished task stays listed this long, so its end can be shown
_UNFINISHED = {"pending", "running", "waiting_for_confirmation", "waiting_for_external_work"}
_ENDED = {"completed", "failed", "cancelled"}

_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"


def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def read_timers(path: Path, now: float) -> list[dict]:
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    timers = []
    for item in items if isinstance(items, list) else []:
        start, end = _epoch(item.get("created_at")), _epoch(item.get("fire_at"))
        if start is None or end is None or end <= start:
            continue
        if item.get("kind") == "reminder":
            if end - now > REMINDER_LEAD:
                continue
            start = max(start, end - REMINDER_LEAD)
        timers.append({"id": str(item.get("id")), "start": start, "end": end})
    return sorted(timers, key=lambda t: t["end"])


def read_tasks(folder: Path, now: float) -> list[dict]:
    tasks = []
    for path in sorted(folder.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        touched = max(_epoch(record.get("updated_at")) or 0, _epoch(record.get("created_at")) or 0)
        status = record.get("status")
        live = status in _UNFINISHED and now - touched < TASK_STALE
        just_ended = status in _ENDED and now - touched < TASK_ENDED
        if live or just_ended:
            tasks.append({"id": str(record.get("task_id") or path.stem),
                          "status": record["status"]})
    return tasks


class DoNotDisturb:
    """KDE's notification inhibition, held for as long as this holds it."""

    def __init__(self):
        self._conn = None
        self._cookie = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    @staticmethod
    def _call(method, signature, args):
        from jeepney import DBusAddress, new_method_call
        return new_method_call(DBusAddress("/org/freedesktop/Notifications",
                                           bus_name="org.freedesktop.Notifications",
                                           interface="org.freedesktop.Notifications"),
                               method, signature, args)

    def hold(self) -> None:
        if self._conn is not None:
            return
        try:
            from jeepney.io.blocking import open_dbus_connection
            conn = open_dbus_connection(bus="SESSION")
            reply = conn.send_and_get_reply(
                self._call("Inhibit", "ssa{sv}", ("jarvis", "Focus session", {})))
            self._conn, self._cookie = conn, reply.body[0]
        except Exception:
            self._conn = None

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.send_and_get_reply(self._call("UnInhibit", "u", (self._cookie,)))
        except Exception:
            pass
        finally:
            conn.close()


def _dbus_monitor() -> list[str]:
    return ["dbus-monitor", "--session", "--profile",
            "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"]


class SkyFeed:
    def __init__(self, port: int = PORT, wait: float = _WAIT, retry: float = _RETRY,
                 scheduled: Path = _RUNTIME / "scheduled.json",
                 tasks: Path = _RUNTIME / "tasks" / "active", monitor_cmd=None,
                 focus_file: Path | None = None, dnd=None):
        self._cond = threading.Condition()
        self._seq = 0
        self._timers: list[dict] = []
        self._tasks: list[dict] = []
        self._notifications = 0
        self._focus: dict | None = None
        self._focus_file = Path(focus_file) if focus_file else None
        self.dnd = dnd or DoNotDisturb()
        self._port, self._wait, self._retry = port, wait, retry
        self._scheduled, self._tasks_dir = Path(scheduled), Path(tasks)
        self._monitor_cmd = monitor_cmd or _dbus_monitor
        self._monitor: subprocess.Popen | None = None
        self._stopping = threading.Event()
        self._listening = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        server = self._server
        return server.server_address[1] if server else self._port

    def listening(self, timeout: float | None = None) -> bool:
        return self._listening.wait(timeout)

    def snapshot(self) -> dict:
        with self._cond:
            return self._snapshot()

    def _snapshot(self) -> dict:
        return {"seq": self._seq, "timers": self._timers, "tasks": self._tasks,
                "notifications": self._notifications, "focus": self._focus}

    def next_after(self, seq: int) -> dict:
        """The snapshot once it's newer than `seq`, or the current one after a while."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq, timeout=self._wait)
            return self._snapshot()

    def scan(self) -> None:
        """Look at the timer, task and focus files; a change makes a new snapshot."""
        now = time.time()
        timers, tasks = read_timers(self._scheduled, now), read_tasks(self._tasks_dir, now)
        session = focus.active(self._focus_file, now)
        focusing = {"started": session["started"], "until": session["until"]} if session else None
        if focusing:
            self.dnd.hold()
        else:
            self.dnd.release()
        with self._cond:
            if timers != self._timers or tasks != self._tasks or focusing != self._focus:
                self._timers, self._tasks, self._focus = timers, tasks, focusing
                self._seq += 1
                self._cond.notify_all()

    def notified(self) -> None:
        with self._cond:
            self._notifications += 1
            self._seq += 1
            self._cond.notify_all()

    # -- watchers -----------------------------------------------------------

    def _scan_loop(self) -> None:
        while not self._stopping.is_set():
            self.scan()
            self._stopping.wait(_SCAN)

    def _watch_notifications(self) -> None:
        while not self._stopping.is_set():
            try:
                self._monitor = subprocess.Popen(self._monitor_cmd(), stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, text=True)
            except OSError:
                return
            for line in self._monitor.stdout:
                fields = line.split()
                if len(fields) >= 2 and fields[0] == "mc" and fields[-1] == "Notify":
                    self.notified()
            self._stopping.wait(5)      # the bus went away: try again in a bit

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._stopping.clear()
        self.scan()
        for target in (self._serve, self._scan_loop, self._watch_notifications):
            threading.Thread(target=target, daemon=True).start()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self._port), _handler(self))
            except OSError:
                self._stopping.wait(self._retry)
                continue
            self._server = server
            self._listening.set()
            server.serve_forever(poll_interval=0.5)
            server.server_close()
            return

    def stop(self) -> None:
        self._stopping.set()
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.terminate()
        self.dnd.release()
        with self._cond:
            self._cond.notify_all()


def _handler(feed: SkyFeed):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/sky":
                self.send_error(404)
                return
            try:
                after = int(parse_qs(url.query).get("after", ["-1"])[0])
            except ValueError:
                after = -1
            body = json.dumps(feed.next_after(after)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    return Handler


def main() -> None:
    feed = SkyFeed()
    feed.start()
    for _ in sys.stdin:     # nothing to read; EOF means the launcher is gone
        pass
    feed.stop()


if __name__ == "__main__":
    main()
