"""
Control a running JARVIS launcher from the command line (Linux).

Windows uses global hotkeys; Wayland doesn't let ordinary apps grab keys
system-wide, so the launcher listens for signals instead. Bind these commands
to keyboard shortcuts in your desktop settings (KDE: System Settings →
Keyboard → Shortcuts → Add New → Command or Script).

Usage:
    uv run friday_ctl ptt             # wake without the wake word / interrupt a turn
    uv run friday_ctl restart         # relaunch to pick up code changes
    uv run friday_ctl kill            # shut JARVIS down
    uv run friday_ctl status

    uv run friday_ctl ask "..."       # type to JARVIS; the reply is printed, never spoken

    uv run friday_ctl quiet on        # no speech, no wake word (until the Wi-Fi changes)
    uv run friday_ctl quiet off       # allow speech here, even on school Wi-Fi
    uv run friday_ctl quiet auto      # back to automatic (quiet on school Wi-Fi)
    uv run friday_ctl quiet toggle
    uv run friday_ctl school add      # mark the current Wi-Fi as school ([name] optional)
    uv run friday_ctl school remove
    uv run friday_ctl school list
"""

import json
import os
import signal
import socket
import sys
from pathlib import Path

from friday import plat, quiet

RUNTIME = Path(__file__).parent / "runtime"
PID_FILE = RUNTIME / "friday.pid"
CONTROL_SOCKET = RUNTIME / "friday.sock"

_SIGNALS = {
    "ptt": "SIGUSR2",
    "restart": "SIGHUP",
    "kill": "SIGTERM",
}


def _running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)   # existence check, sends nothing
        return pid
    except (OSError, ValueError):
        return None


def request(payload: dict, timeout: float = 75.0) -> dict:
    """One request/response over the launcher's control socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(CONTROL_SOCKET))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError) as e:
        return {"error": f"JARVIS is not reachable ({type(e).__name__})"}


def ask(text: str) -> tuple[str, str | None]:
    """Send a typed request. Returns (reply, error)."""
    response = request({"cmd": "ask", "text": text})
    reply = (response.get("text") or "").strip()
    error = response.get("error")
    if not reply and not error:
        reply = "Done, sir."   # an action that succeeded with nothing to add
    return reply, error


def _describe_quiet(state: dict) -> str:
    where = f"on '{state['ssid']}'" if state["ssid"] else "not on Wi-Fi"
    mode = "QUIET (text only)" if state["quiet"] else "normal (voice on)"
    return f"JARVIS is {mode} — {state['reason']}, {where}."


def _cmd_quiet(args: list[str]) -> None:
    action = args[0] if args else "status"
    if action == "on":
        state = quiet.set_override(True)
    elif action == "off":
        state = quiet.set_override(False)
    elif action == "auto":
        state = quiet.set_override(None)
    elif action == "toggle":
        state = quiet.set_override(not quiet.status()["quiet"])
    elif action == "status":
        state = quiet.status()
    else:
        print("Usage: friday_ctl quiet [on|off|auto|toggle|status]")
        sys.exit(2)
    message = _describe_quiet(state)
    print(message)
    if action != "status":
        plat.notify("JARVIS quiet mode " + ("on" if state["quiet"] else "off"), message)


def _cmd_school(args: list[str]) -> None:
    action = args[0] if args else "list"
    name = " ".join(args[1:]) or None
    try:
        if action == "add":
            print(f"'{quiet.add_school(name)}' is now a school network — "
                  "JARVIS goes quiet whenever you're on it.")
        elif action == "remove":
            print(f"'{quiet.remove_school(name)}' is no longer a school network.")
        elif action == "list":
            networks = quiet.status()["school_ssids"]
            print("School networks: " + (", ".join(networks) if networks else "none yet"))
        else:
            print("Usage: friday_ctl school [add|remove|list] [network name]")
            sys.exit(2)
    except ValueError as e:
        print(f"Couldn't do that: {e}")
        sys.exit(1)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]

    if command == "quiet":
        return _cmd_quiet(args)
    if command == "school":
        return _cmd_school(args)
    if command == "ask":
        text = " ".join(args).strip()
        if not text:
            print('Usage: friday_ctl ask "your request"')
            sys.exit(2)
        reply, error = ask(text)
        print(reply or f"JARVIS couldn't answer: {error}")
        sys.exit(1 if error and not reply else 0)

    if command not in (*_SIGNALS, "status"):
        print(__doc__.strip())
        sys.exit(2)
    pid = _running_pid()
    if pid is None:
        print("JARVIS is not running.")
        sys.exit(1)
    if command == "status":
        print(f"JARVIS is running (PID {pid}).")
        print(_describe_quiet(quiet.status()))
        return
    os.kill(pid, getattr(signal, _SIGNALS[command]))
    print(f"Sent {command} to JARVIS (PID {pid}).")


if __name__ == "__main__":
    main()
