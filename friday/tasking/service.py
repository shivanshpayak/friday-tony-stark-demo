"""High-level task orchestration."""
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .models import TaskRecord
from .store import create_task, load_task
from .executor import set_completion_callback
from friday.config import TASK_STATE_DIR

logger = logging.getLogger("friday-agent")

_GLOBAL_TOOLSET = None
_WATCHER_TASK = None

# Tasks whose callback has already fired — prevents re-notification.
_NOTIFIED_TASKS: set[str] = set()

_POLL_INTERVAL = 3.0  # seconds between file-system polls

def register_toolset(toolset):
    """Register the MCP toolset so background executors can use it."""
    global _GLOBAL_TOOLSET
    _GLOBAL_TOOLSET = toolset


async def _file_watcher_loop():
    """Poll ALL task JSON files in the task directory for completion.

    This scans the filesystem directly rather than relying on an in-memory
    registry, so it catches tasks created by any process — the agent,
    the MCP server (ask_claude), or the standalone executor.
    """
    import inspect
    from . import executor   # module ref so we always read the live _CALLBACK

    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            for json_file in TASK_STATE_DIR.glob("*.json"):
                task_id = json_file.stem
                if task_id in _NOTIFIED_TASKS:
                    continue  # already handled

                task = load_task(task_id)
                if not task:
                    continue

                if task.status in ("completed", "failed"):
                    _NOTIFIED_TASKS.add(task_id)
                    logger.info("File-watcher: task %s → %s", task_id, task.status)

                    cb = executor._CALLBACK
                    if cb:
                        try:
                            if inspect.iscoroutinefunction(cb):
                                await cb(task)
                            else:
                                cb(task)
                        except Exception as e:
                            logger.error("Task completion callback error for %s: %s",
                                         task_id, e)
        except Exception as e:
            logger.error("File-watcher loop error: %s", e)


def start_worker():
    """Start the background file-watcher loop."""
    global _WATCHER_TASK
    if _WATCHER_TASK is None:
        # Seed with already-finished tasks so we don't re-notify on boot.
        for json_file in TASK_STATE_DIR.glob("*.json"):
            task = load_task(json_file.stem)
            if task and task.status in ("completed", "failed"):
                _NOTIFIED_TASKS.add(task.task_id)
        _WATCHER_TASK = asyncio.create_task(_file_watcher_loop())

def start_task(goal: str, source: str = "voice") -> str:
    """Creates a new TaskRecord, launches the standalone executor in a visible
    CMD window, and registers it with the file-watcher for completion callback."""
    task_id = f"task_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"

    task = TaskRecord(
        task_id=task_id,
        goal=goal,
        status="pending",
        mode="planner",
        source=source,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        steps=[]
    )
    create_task(task)

    # Launch a detached visible terminal window running the standalone executor.
    import os
    import sys
    from friday import plat
    if plat.IS_WINDOWS:
        cmd = f'start cmd /k "{sys.executable} -m friday.tasking.standalone_executor {task_id}"'
        os.system(cmd)
    else:
        plat.open_terminal(
            [sys.executable, "-m", "friday.tasking.standalone_executor", task_id],
            cwd=str(Path(__file__).resolve().parents[2]),
        )

    return task_id

def get_task_status(task_id: str) -> TaskRecord | None:
    return load_task(task_id)

def summarize_task(task_id: str) -> str:
    task = load_task(task_id)
    if not task:
        return "I couldn't find that task."
    if task.final_summary:
        return task.final_summary
    return f"Task is currently {task.status}."
