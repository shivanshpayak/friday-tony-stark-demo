"""
Install/uninstall JARVIS as a login startup item.

Windows: a Task Scheduler "at logon" task.
Linux:   an XDG autostart entry (~/.config/autostart/jarvis.desktop).

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


AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "jarvis.desktop"


def _install_linux():
    script = PROJECT_DIR / "start_friday.sh"
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={TASK_NAME}\n"
        f'Exec="{script}"\n'
        f"Path={PROJECT_DIR}\n"
        "Terminal=false\n"
        "X-KDE-autostart-after=panel\n",
        encoding="utf-8",
    )
    print(f"Installed '{TASK_NAME}' to run at login.")
    print(f"  Entry: {AUTOSTART_FILE}")


def _uninstall_linux():
    if AUTOSTART_FILE.exists():
        AUTOSTART_FILE.unlink()
        print(f"Removed '{TASK_NAME}' from startup.")
    else:
        print(f"'{TASK_NAME}' was not installed.")


def install():
    if sys.platform != "win32":
        return _install_linux()
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
    if sys.platform != "win32":
        return _uninstall_linux()
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
