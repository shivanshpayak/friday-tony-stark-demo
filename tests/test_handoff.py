"""Cross-OS handoff: a task left on one OS runs once, on the other, and only
while it is still fresh."""
from datetime import datetime, timedelta, timezone

import pytest

from friday import handoff, plat
from friday.tools import frc


@pytest.fixture
def queue(tmp_path):
    return handoff.FolderQueue(tmp_path)


def test_task_is_only_claimed_by_the_os_it_was_left_for(queue, monkeypatch):
    monkeypatch.setattr(handoff, "current_os", lambda: "linux")
    handoff.leave_task("FRC mode", "windows", queue)
    assert handoff.claim_pending(queue) == []          # still on Linux: not ours

    monkeypatch.setattr(handoff, "current_os", lambda: "windows")
    (task,) = handoff.claim_pending(queue)
    assert task.request == "FRC mode" and task.created_on == "linux"


def test_a_finished_task_never_runs_twice(queue, monkeypatch):
    monkeypatch.setattr(handoff, "current_os", lambda: "windows")
    handoff.leave_task("FRC mode", "windows", queue)
    (task,) = handoff.claim_pending(queue)
    queue.finish(task, "done", "opened everything")
    assert handoff.claim_pending(queue) == []
    assert queue.pending() == []


def test_stale_tasks_are_retired_not_run(queue, monkeypatch):
    """'FRC mode' from last week must not fire on some unrelated boot."""
    monkeypatch.setattr(handoff, "current_os", lambda: "windows")
    old = handoff.HandoffTask(
        request="FRC mode", target_os="windows",
        created_at=(datetime.now(timezone.utc) - handoff.MAX_AGE
                    - timedelta(minutes=1)).isoformat())
    queue.add(old)
    assert handoff.claim_pending(queue) == []
    assert queue.pending() == []                        # retired, not left to rot


def test_default_target_is_the_other_os(queue, monkeypatch):
    monkeypatch.setattr(handoff, "current_os", lambda: "linux")
    assert handoff.leave_task("x", None, queue).target_os == "windows"
    with pytest.raises(ValueError):
        handoff.leave_task("x", "macos", queue)


def test_unreachable_queue_is_reported_not_swallowed(monkeypatch):
    class Offline:
        def pending(self):
            raise OSError("network is down")

    with pytest.raises(RuntimeError):
        handoff.claim_pending(Offline(), retries=2, delay=0)


# --- FRC mode off Windows: hand off, then reboot -------------------------------

@pytest.fixture
def linux_frc(monkeypatch):
    calls = {"left": [], "restarts": 0}
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "can_restart_into_windows", lambda: None)
    monkeypatch.setattr(plat, "restart_into_windows",
                        lambda *a, **k: calls.__setitem__("restarts", calls["restarts"] + 1))
    monkeypatch.setattr(handoff, "leave_task",
                        lambda request, target: calls["left"].append((request, target)))
    return calls


def test_frc_mode_on_linux_leaves_a_note_then_restarts(linux_frc):
    frc._start_frc_mode("")
    assert linux_frc["left"] == [("FRC mode", "windows")]
    assert linux_frc["restarts"] == 1


def test_no_restart_if_the_note_could_not_be_saved(linux_frc, monkeypatch):
    """Rebooting without the note would land in Windows with nothing to do."""
    def offline(request, target):
        raise OSError("network is down")

    monkeypatch.setattr(handoff, "leave_task", offline)
    assert "staying put" in frc._start_frc_mode("")
    assert linux_frc["restarts"] == 0


def test_no_note_if_the_restart_is_not_possible(linux_frc, monkeypatch):
    """A queued task with no reboot would ambush the next Windows boot."""
    monkeypatch.setattr(plat, "can_restart_into_windows", lambda: "helper missing")
    assert "can't switch" in frc._start_frc_mode("")
    assert linux_frc["left"] == [] and linux_frc["restarts"] == 0
