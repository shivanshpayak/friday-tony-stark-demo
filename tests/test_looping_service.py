"""Tests for the loop service: clamping, outcome->task mapping, cancellation."""
from datetime import datetime

from friday.looping import service
from friday.looping.runner import LoopResult
from friday.tasking.models import TaskRecord
from friday.tasking.store import create_task, load_task, delete_task


def test_clamp_enforces_floor_and_ceiling():
    interval, attempts, seconds = service._clamp(0.1, 999, 99_999)
    assert interval == service.INTERVAL_MIN
    assert attempts == service.MAX_ATTEMPTS_CEILING
    assert seconds == service.MAX_SECONDS_CEILING


def _make_task(task_id):
    now = datetime.now().isoformat()
    create_task(TaskRecord(task_id=task_id, goal="reconnect your wifi",
                           status="running", created_at=now, updated_at=now))


def test_finalize_success_marks_completed():
    tid = "loop_test_success"
    _make_task(tid)
    try:
        service._finalize(tid, LoopResult("success", 2, "ok"))
        t = load_task(tid)
        assert t.status == "completed"
        assert "reconnect your wifi" in t.final_summary
    finally:
        delete_task(tid)


def test_finalize_exhausted_marks_failed_without_timeout_prefix():
    tid = "loop_test_timeout"
    _make_task(tid)
    try:
        service._finalize(tid, LoopResult("exhausted_time", 9, "no success within 120s"))
        t = load_task(tid)
        assert t.status == "failed"
        # Must NOT use the Claude-specific TIMEOUT:/TOKEN_LIMIT: callback branches.
        assert not t.final_summary.startswith("TIMEOUT:")
        assert not t.final_summary.startswith("TOKEN_LIMIT:")
    finally:
        delete_task(tid)


def test_finalize_cancelled_marks_cancelled():
    tid = "loop_test_cancel"
    _make_task(tid)
    try:
        service._finalize(tid, LoopResult("cancelled", 1, "cancelled"))
        t = load_task(tid)
        assert t.status == "cancelled"
    finally:
        delete_task(tid)


def test_stop_loop_with_no_active_loops():
    # Ensure a clean slate, then stopping reports nothing running.
    service.stop_loop("all")
    msg = service.stop_loop("all")
    assert "No background loops" in msg
