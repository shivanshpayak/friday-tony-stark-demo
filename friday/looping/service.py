"""Glue: turn a validated loop spec into a background, TaskRecord-driven loop.

Reuses friday/tasking's TaskRecord + store, so the existing file-watcher callback
(_on_task_finished in agent_friday.py) announces completed/failed loops. Cancelled
loops are not announced (stop_loop already gives a spoken ack).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

from friday.looping.registry import build_action, build_check
from friday.looping.runner import LoopResult, run_loop
from friday.tasking.models import TaskRecord
from friday.tasking.store import create_task, load_task, update_task

logger = logging.getLogger("friday-agent")

# --- safety bounds ---
INTERVAL_MIN = 1.0
MAX_ATTEMPTS_CEILING = 60
MAX_SECONDS_CEILING = 900.0
MAX_CONCURRENT_LOOPS = 3

# active loops: task_id -> cancel Event
_ACTIVE: Dict[str, threading.Event] = {}
_ACTIVE_LOCK = threading.Lock()


def _clamp(interval, max_attempts, max_seconds):
    interval = max(INTERVAL_MIN, float(interval))
    max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS_CEILING))
    max_seconds = max(1.0, min(float(max_seconds), MAX_SECONDS_CEILING))
    return interval, max_attempts, max_seconds


def active_loop_count() -> int:
    with _ACTIVE_LOCK:
        return sum(1 for e in _ACTIVE.values() if not e.is_set())


def start_loop(
    *,
    action: str,
    action_args: Dict[str, Any],
    check: str,
    check_args: Dict[str, Any],
    interval: float = 3.0,
    max_attempts: int = 10,
    max_seconds: float = 120.0,
    goal_text: str = "",
    source: str = "voice",
) -> str:
    # 1. resolve against the registries (raises ValueError with a friendly message)
    action_fn = build_action(action, action_args)
    check_fn = build_check(check, check_args)

    # 2. concurrency cap
    if active_loop_count() >= MAX_CONCURRENT_LOOPS:
        return (
            f"I'm already running {MAX_CONCURRENT_LOOPS} background loops, sir. "
            "Say 'stop' to clear them first."
        )

    # 3. clamp bounds
    interval, max_attempts, max_seconds = _clamp(interval, max_attempts, max_seconds)

    # 4. task record
    task_id = f"loop_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"
    now = datetime.now().isoformat()
    task = TaskRecord(
        task_id=task_id,
        goal=goal_text or f"{action} until {check}",
        status="running",
        mode="planner",
        source=source,  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
        steps=[],
    )
    create_task(task)

    cancel = threading.Event()
    with _ACTIVE_LOCK:
        _ACTIVE[task_id] = cancel

    threading.Thread(
        target=_run_and_finalize,
        args=(task_id, action_fn, check_fn, interval, max_attempts, max_seconds, cancel),
        daemon=True,
    ).start()

    return (
        f"Working on it in the background, sir — I'll keep trying up to "
        f"{max_attempts} times and let you know."
    )


def _run_and_finalize(task_id, action_fn, check_fn, interval, max_attempts, max_seconds, cancel):
    try:
        result = run_loop(
            action_fn, check_fn,
            interval=interval, max_attempts=max_attempts, max_seconds=max_seconds,
            cancel_event=cancel,
        )
    except Exception as e:  # runner shouldn't raise, but never die silently
        logger.exception("Loop %s crashed", task_id)
        result = LoopResult("aborted", 0, f"{type(e).__name__}: {e}")

    _finalize(task_id, result)
    with _ACTIVE_LOCK:
        _ACTIVE.pop(task_id, None)


def _finalize(task_id: str, result: LoopResult) -> None:
    task = load_task(task_id)
    if not task:
        return
    goal = task.goal
    task.updated_at = datetime.now().isoformat()
    if result.outcome == "success":
        task.status = "completed"
        task.final_summary = f"Success: {goal} (after {result.attempts} attempt(s))."
    elif result.outcome in ("exhausted_attempts", "exhausted_time"):
        # NOTE: deliberately NOT the "TIMEOUT:"/"TOKEN_LIMIT:" prefixes — those hit
        # Claude-specific branches in _on_task_finished. Use the generic failed path.
        task.status = "failed"
        task.final_summary = f"Couldn't complete: {goal}. {result.reason}"
    elif result.outcome == "cancelled":
        task.status = "cancelled"
        task.final_summary = "Stopped."
    else:  # aborted
        task.status = "failed"
        task.final_summary = f"Couldn't complete: {goal}. {result.reason}"
    update_task(task)


def stop_loop(target: str = "all") -> str:
    with _ACTIVE_LOCK:
        if target == "all":
            events = list(_ACTIVE.values())
            for e in events:
                e.set()
            n = len(events)
        else:
            e = _ACTIVE.get(target)
            if e:
                e.set()
            n = 1 if e else 0
    if n == 0:
        return "No background loops are running, sir."
    return f"Stopped {n} loop{'s' if n != 1 else ''}, sir."
