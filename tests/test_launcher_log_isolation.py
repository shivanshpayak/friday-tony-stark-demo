"""Tests must never write to the real logs/friday.log.

friday_launcher configures a file handler at import. Test imports used to append
fake entries to the live log — "SESSION_FATAL", "PID 999999", "Restart aborted" —
which would mislead anyone debugging JARVIS from it. FRIDAY_LOG_DIR redirects the
log; tests/conftest.py points it at a temp dir for the whole session.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_log_dir_env_redirects_launcher_log(tmp_path):
    code = "import friday_launcher; print(friday_launcher.LOG_FILE)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
        timeout=240, env={**os.environ, "FRIDAY_LOG_DIR": str(tmp_path)},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert Path(out.stdout.strip().splitlines()[-1]) == tmp_path / "friday.log"
    assert (tmp_path / "friday.log").exists()


def test_test_session_does_not_use_the_real_log_dir():
    assert os.environ.get("FRIDAY_LOG_DIR"), "conftest must set FRIDAY_LOG_DIR"
    assert Path(os.environ["FRIDAY_LOG_DIR"]).resolve() != (REPO / "logs").resolve()
