"""What the wallpaper draws around its black hole (friday/sky_feed.py)."""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import pytest

from friday.sky_feed import SkyFeed, read_tasks, read_timers


def _iso(seconds_from_now):
    return (datetime.now() + timedelta(seconds=seconds_from_now)).isoformat()


def _scheduled(tmp_path, items):
    path = tmp_path / "scheduled.json"
    path.write_text(json.dumps(items))
    return path


def test_timers_carry_their_start_and_end(tmp_path):
    path = _scheduled(tmp_path, [{"id": "t1", "kind": "timer", "created_at": _iso(-60),
                                  "fire_at": _iso(540), "message": "", "label": ""}])
    [timer] = read_timers(path, time.time())
    assert timer["id"] == "t1"
    assert timer["end"] - timer["start"] == pytest.approx(600, abs=1)


def test_reminders_join_only_near_the_end(tmp_path):
    path = _scheduled(tmp_path, [
        {"id": "soon", "kind": "reminder", "created_at": _iso(-7200), "fire_at": _iso(300)},
        {"id": "tomorrow", "kind": "reminder", "created_at": _iso(-60), "fire_at": _iso(86400)},
    ])
    [soon] = read_timers(path, time.time())
    assert soon["id"] == "soon"
    assert soon["end"] - soon["start"] == pytest.approx(15 * 60, abs=1)   # not two hours


def test_a_broken_store_is_an_empty_sky(tmp_path):
    path = tmp_path / "scheduled.json"
    path.write_text("{not json")
    assert read_timers(path, time.time()) == []
    assert read_timers(tmp_path / "missing.json", time.time()) == []


def _task(folder, task_id, status, updated_seconds_ago):
    when = _iso(-updated_seconds_ago)
    (folder / f"{task_id}.json").write_text(json.dumps(
        {"task_id": task_id, "status": status, "created_at": when, "updated_at": when, "goal": "x"}))


def test_live_tasks_orbit_and_just_ended_ones_say_how(tmp_path):
    _task(tmp_path, "loop_live", "running", 30)
    _task(tmp_path, "claude_waiting", "waiting_for_external_work", 600)
    _task(tmp_path, "claude_just_failed", "failed", 30)
    _task(tmp_path, "claude_done_long_ago", "completed", 3600)
    _task(tmp_path, "orchestrator_left_behind", "running", 30 * 86400)
    tasks = {t["id"]: t["status"] for t in read_tasks(tmp_path, time.time())}
    assert tasks == {"loop_live": "running", "claude_waiting": "waiting_for_external_work",
                     "claude_just_failed": "failed"}


# dbus-monitor --profile: one line per message, never its contents.
_FAKE_MONITOR = """
import time
print("sig 1.0 2 org.freedesktop.DBus :1.9 /org/freedesktop/DBus org.freedesktop.DBus NameAcquired", flush=True)
for i in range(3):
    time.sleep(0.2)
    print(f"mc 1.{i} {10+i} :1.4 :1.7 /org/freedesktop/Notifications org.freedesktop.Notifications Notify", flush=True)
print("mc 2.0 20 :1.4 :1.7 /org/freedesktop/Notifications org.freedesktop.Notifications CloseNotification", flush=True)
time.sleep(30)
"""


class _FakeDnd:
    def __init__(self):
        self.held = False

    def hold(self):
        self.held = True

    def release(self):
        self.held = False


@pytest.fixture
def feed(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    f = SkyFeed(port=0, wait=5, scheduled=tmp_path / "scheduled.json", tasks=tasks,
                monitor_cmd=lambda: [sys.executable, "-c", _FAKE_MONITOR],
                focus_file=tmp_path / "focus.json", dnd=_FakeDnd())
    f.start()
    assert f.listening(5)
    yield f, tmp_path
    f.stop()


def _get(f, after):
    with urllib.request.urlopen(f"http://127.0.0.1:{f.port}/sky?after={after}", timeout=10) as r:
        return json.loads(r.read())


def test_notifications_are_counted_not_read(feed):
    f, _ = feed
    deadline = time.monotonic() + 5
    while f.snapshot()["notifications"] < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assert f.snapshot()["notifications"] == 3      # CloseNotification and the rest don't count


def test_a_new_timer_answers_the_long_poll(feed):
    f, folder = feed
    seq = _get(f, -1)["seq"]
    started = time.monotonic()
    _scheduled(folder, [{"id": "pasta", "kind": "timer", "created_at": _iso(0), "fire_at": _iso(600)}])
    snap = _get(f, seq)
    while not snap["timers"] and time.monotonic() - started < 5:    # a notification may land first
        snap = _get(f, snap["seq"])
    assert [t["id"] for t in snap["timers"]] == ["pasta"]
    assert time.monotonic() - started < 3


def test_a_focus_session_holds_do_not_disturb_and_tells_the_wallpaper(feed):
    from friday import focus
    f, folder = feed
    assert not f.dnd.held and f.snapshot()["focus"] is None
    record = focus.begin(45, ["Discord"], 0, path=folder / "focus.json")
    f.scan()
    assert f.dnd.held
    assert f.snapshot()["focus"] == {"started": record["started"], "until": record["until"]}
    focus.finish(path=folder / "focus.json")
    f.scan()
    assert not f.dnd.held and f.snapshot()["focus"] is None
