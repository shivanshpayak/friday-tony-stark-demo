"""
Headless Claude CLI delegation — Phase 6b.

Launches `claude -p "<prompt>"` as a background subprocess with file-write
permissions scoped to `runtime/claude_output/`.  Writes the result to a task
JSON so the file-watcher in service.py fires the completion callback and
Jarvis speaks a summary of what Claude did.
"""

import logging
import os
import sys
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from friday.config import TASK_STATE_DIR
from friday.tasking.models import TaskRecord
from friday.tasking.store import create_task, save_task

logger = logging.getLogger("friday-agent")

_REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_OUTPUT_DIR = _REPO_ROOT / "runtime" / "claude_output"
CLAUDE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _list_files_before(directory: Path) -> set[str]:
    """Snapshot filenames in a directory."""
    if not directory.exists():
        return set()
    return {f.name for f in directory.iterdir() if f.is_file()}


def _run_claude(task_id: str, prompt: str):
    """Run claude CLI in a background thread and update the task JSON."""
    task = None
    try:
        from friday.tasking.store import load_task as _load
        task = _load(task_id)
        if not task:
            logger.error("ask_claude: task %s not found", task_id)
            return

        task.status = "running"
        task.updated_at = datetime.now().isoformat()
        save_task(task)

        # Snapshot files before Claude runs so we can detect new ones.
        files_before = _list_files_before(CLAUDE_OUTPUT_DIR)

        # Augment prompt to tell Claude where to save files.
        full_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT: Save any files you create to this directory: "
            f"{CLAUDE_OUTPUT_DIR}\n"
            f"After you are done, output ONLY what you created in one short "
            f"sentence. Example: 'Created a D20 dice roller.' "
            f"Do NOT describe features, implementation details, or file paths."
        )

        # Use shell=True so Windows resolves claude.cmd from PATH.
        # The prompt is passed via stdin to avoid shell escaping issues.
        result = subprocess.run(
            "claude -p --dangerously-skip-permissions",
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=180,
            shell=True,
            cwd=str(CLAUDE_OUTPUT_DIR),
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"},
        )

        output = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        all_output = f"{output}\n{stderr}".lower()

        # Detect token/rate limit failures
        token_exhausted = any(phrase in all_output for phrase in (
            "rate limit",
            "token limit",
            "quota exceeded",
            "insufficient_quota",
            "billing",
            "credit",
            "usage limit",
            "max tokens",
            "context window",
            "too many requests",
            "429",
        ))

        if token_exhausted or (result.returncode != 0 and not output):
            if token_exhausted:
                task.status = "failed"
                task.final_summary = (
                    "TOKEN_LIMIT: Claude ran out of tokens or hit a rate limit. "
                    f"The original request was: {prompt[:200]}"
                )
                logger.warning("ask_claude: task %s hit token/rate limit", task_id)
            else:
                error_msg = stderr or "Claude returned no output."
                task.status = "failed"
                task.final_summary = f"Claude failed: {error_msg[:500]}"
                logger.error("ask_claude: task %s failed with returncode %d", task_id, result.returncode)
        else:
            # Detect new files created.
            files_after = _list_files_before(CLAUDE_OUTPUT_DIR)
            new_files = files_after - files_before

            # Build a short summary — no paths, no filenames, no details.
            if output:
                final_summary = output[:500]
            elif new_files:
                final_summary = f"Created {len(new_files)} file(s)."
            else:
                final_summary = "Done."

            task.status = "completed"
            task.final_summary = final_summary[:2000]
            logger.info("ask_claude: task %s completed, new files: %s", task_id, new_files or "none")

    except subprocess.TimeoutExpired:
        logger.error("ask_claude: task %s timed out", task_id)
        if task:
            task.status = "failed"
            task.final_summary = "TIMEOUT: Claude took too long and was stopped after 3 minutes."
    except Exception as e:
        logger.error("ask_claude: task %s failed: %s", task_id, e)
        if task:
            task.status = "failed"
            task.final_summary = f"Claude delegation failed: {e}"
    finally:
        if task:
            task.updated_at = datetime.now().isoformat()
            save_task(task)


def register(mcp):
    @mcp.tool()
    def ask_claude(prompt: str) -> str:
        """Delegate a complex question or task to Claude Code running in the
        background. Use this for deep analysis, code generation, research
        synthesis, or anything that benefits from Claude's reasoning.
        Jarvis will speak the result when Claude finishes."""

        task_id = f"claude_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"

        task = TaskRecord(
            task_id=task_id,
            goal=f"Claude delegation: {prompt[:200]}",
            status="pending",
            mode="planner",
            source="voice",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            steps=[],
        )
        create_task(task)

        # The file-watcher in service.py scans the task directory directly,
        # so no in-memory registration is needed. It will detect the completed
        # JSON and fire the callback.

        # Run in a background thread so the tool returns immediately.
        thread = threading.Thread(
            target=_run_claude, args=(task_id, prompt), daemon=True
        )
        thread.start()

        return f"Delegated to Claude (task {task_id}). I'll report back when it's done."

    @mcp.tool()
    def delegate_to_orchestrator(goal: str) -> str:
        """
        Spawn a highly capable background orchestrator that has full access to ALL your MCP tools.
        Use this for highly complex, multi-step workflows like tuning PIDs iteratively, 
        reading multiple files and pushing massive code refactors, or running test suites.
        This physically opens a new visible command window on the user's screen so they can watch it work.
        """
        task_id = f"orchestrator_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"

        task = TaskRecord(
            task_id=task_id,
            goal=goal,
            status="pending",
            mode="planner",
            source="voice",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            steps=[],
        )
        create_task(task)

        # Launch the standalone_executor in a new visible cmd window
        try:
            # We use CREATE_NEW_CONSOLE so the user can physically watch the agent think
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_CONSOLE

            subprocess.Popen(
                [sys.executable, "-m", "friday.tasking.standalone_executor", task_id],
                cwd=str(_REPO_ROOT),
                creationflags=creation_flags
            )
            return f"Successfully spawned the background orchestrator. You should see a new command window pop up to handle task '{goal[:50]}...'."
        except Exception as e:
            logger.error("Failed to spawn orchestrator: %s", e)
            return f"Error spawning orchestrator: {e}"
