"""
Install/uninstall JARVIS as a Windows startup task.

Usage:
    python install_startup.py install
    python install_startup.py uninstall
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME = "JARVIS Voice Assistant"
PROJECT_DIR = Path(__file__).parent.resolve()
# Use pythonw to avoid a console window
PYTHON_EXE = Path(sys.executable).parent / "pythonw.exe"
UV_MODULE = "uv"


def install():
    command = f'"{PYTHON_EXE}" -m {UV_MODULE} run friday_start'
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", command,
            "/sc", "onlogon",
            "/rl", "limited",
            "/f",
        ],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Installed '{TASK_NAME}' to run at login.")
        print(f"  Command: {command}")
        print(f"  Working dir: {PROJECT_DIR}")
    else:
        print(f"Failed to create task: {result.stderr}")
        sys.exit(1)


def uninstall():
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Removed '{TASK_NAME}' from startup.")
    else:
        print(f"Failed to remove task: {result.stderr}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print("Usage: python install_startup.py [install|uninstall]")
        sys.exit(1)

    if sys.argv[1] == "install":
        install()
    else:
        uninstall()
