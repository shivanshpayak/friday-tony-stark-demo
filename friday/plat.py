"""Cross-platform shims for the handful of OS services JARVIS touches.

Windows keeps its original implementations (winsound, os.startfile, taskkill,
the Win32 clipboard). Linux uses PipeWire/PulseAudio CLI players, xdg-open,
process groups, and wl-clipboard / Klipper.

Deliberately free of any MCP/LiveKit import: the launcher process loads this
module and must not pull in the tool server's dependency tree.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

_DEVNULL = subprocess.DEVNULL
# Windows: keep console programs we spawn from flashing a window. 0 elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- voice fingerprint -----------------------------------------------------

def voice_embedding_path() -> Path:
    """This OS's speaker fingerprint (written by enroll_voice.py).

    One file per OS. The dual-boot installs share this repo through git, and
    with a single voice_embedding.npy an enrollment on one OS replaced the
    other's fingerprint on the next pull. Each side now enrolls, and verifies
    the wake word, against its own file.
    """
    name = "windows" if IS_WINDOWS else "linux"
    return Path(__file__).resolve().parents[1] / f"voice_embedding.{name}.npy"


def _detached(argv: list[str], cwd: str | None = None) -> subprocess.Popen:
    """Spawn a process that outlives us and never touches our stdio."""
    return subprocess.Popen(
        argv, cwd=cwd, stdin=_DEVNULL, stdout=_DEVNULL, stderr=_DEVNULL,
        start_new_session=True, close_fds=True, creationflags=_NO_WINDOW,
    )


def tool_server_env() -> dict[str, str]:
    """Environment for the MCP tool server (server.py).

    The MCP stdio client hands a child only a tiny safe-list (HOME, PATH,
    USER...) unless told otherwise. On Linux that strips DISPLAY,
    WAYLAND_DISPLAY, XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS — so every
    app the tools launch dies without a display, and D-Bus, clipboard and
    screenshot calls fail. The tool server acts on the user's desktop on the
    user's behalf, so it gets the full session environment.
    """
    return dict(os.environ)


# --- sound -----------------------------------------------------------------

_LINUX_PLAYERS = (("pw-play",), ("paplay",), ("aplay", "-q"))
_LINUX_BEEPS = {
    "info": "/usr/share/sounds/freedesktop/stereo/message.oga",
    "error": "/usr/share/sounds/freedesktop/stereo/dialog-error.oga",
}


def play_wav(path) -> None:
    """Play a WAV file without blocking. Raises if no player is available."""
    if IS_WINDOWS:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        return
    for player in _LINUX_PLAYERS:
        if shutil.which(player[0]):
            _detached([*player, str(path)])
            return
    raise RuntimeError("no audio player found (pw-play / paplay / aplay)")


def beep(kind: str = "info") -> None:
    """Short system sound. `kind` is "info" or "error"."""
    if IS_WINDOWS:
        import winsound
        winsound.MessageBeep(
            winsound.MB_ICONHAND if kind == "error" else winsound.MB_ICONASTERISK
        )
        return
    sound = _LINUX_BEEPS.get(kind, _LINUX_BEEPS["info"])
    # aplay can't decode .oga, so only the PipeWire/Pulse players are tried.
    for player in ("pw-play", "paplay"):
        if os.path.exists(sound) and shutil.which(player):
            _detached([player, sound])
            return


# --- rebooting into the other OS -------------------------------------------

# Root-owned helper that makes Windows the next boot only (UEFI one-shot — not
# through GRUB, or BitLocker asks for the recovery key); the user's sudoers
# lets it run without a password. Its source is boot-switch/boot-windows-next,
# put in place by install_boot_switch.py; the sudoers rule is in ~/boot-switch.
_BOOT_WINDOWS_HELPER = "/usr/local/sbin/boot-windows-next"


def can_restart_into_windows() -> str | None:
    """None if a reboot into Windows can be triggered, else why not."""
    if IS_WINDOWS:
        return "already on Windows"
    if not os.path.exists(_BOOT_WINDOWS_HELPER):
        return "the boot-switch helper isn't installed"
    # `sudo -l <cmd>` only asks whether it WOULD be allowed — it runs nothing.
    allowed = subprocess.run(["sudo", "-n", "-l", _BOOT_WINDOWS_HELPER],
                             capture_output=True, timeout=5)
    if allowed.returncode != 0:
        return "I'm not permitted to change the next boot"
    return None


def restart_into_windows(delay: float = 6.0) -> None:
    """Reboot into Windows after `delay` seconds (time for a spoken line).

    Goes through the desktop's own logout, so apps with unsaved work get to
    ask before the session ends. Detached: it must outlive the tool server.
    """
    script = (
        f"sleep {float(delay):.1f}; "
        f"sudo -n {_BOOT_WINDOWS_HELPER} && "
        "qdbus6 org.kde.Shutdown /Shutdown org.kde.Shutdown.logoutAndReboot"
    )
    _detached(["sh", "-c", script])


# The Windows counterpart of the helper above: a Task Scheduler task that runs
# elevated, makes Linux the next boot only (UEFI one-shot) and restarts. Running
# it needs no UAC prompt. Registered once by install_boot_switch.py.
BOOT_LINUX_TASK = "JARVIS Restart into Linux"
# The same, plus a one-time pass that lets Linux log in without the password
# (boot-switch/jarvis-autologin-once). Only JARVIS runs this one: the user has
# just been recognised by voice. The shortcut runs the plain task, so a switch
# by hand still ends at the password.
BOOT_LINUX_VOICE_TASK = "JARVIS Voice Switch to Linux"

_WAIT_THEN_RUN = ("import subprocess, sys, time; "
                  "time.sleep(float(sys.argv[1])); subprocess.run(sys.argv[2:])")


def _run_later(delay: float, argv: list[str]) -> None:
    """Run `argv` after `delay` seconds, in a process that outlives us."""
    _detached([sys.executable, "-c", _WAIT_THEN_RUN, str(delay), *argv])


def can_restart_into_linux() -> str | None:
    """None if a reboot into Linux can be triggered, else why not."""
    if not IS_WINDOWS:
        return "already on Linux"
    if not _task_registered(BOOT_LINUX_TASK):
        return "the boot-switch helper isn't installed"
    return None


def _task_registered(name: str) -> bool:
    return subprocess.run(["schtasks", "/query", "/tn", name],
                          capture_output=True, timeout=5,
                          creationflags=_NO_WINDOW).returncode == 0


def restart_into_linux(delay: float = 6.0) -> None:
    """Reboot into Linux after `delay` seconds (time for a spoken line).

    The task restarts without forcing, so apps with unsaved work get to ask
    before the session ends. Detached: it must outlive the tool server.

    Runs the voice task (Linux then logs in by itself, once) when the
    installer has registered it; otherwise the plain one, so switching keeps
    working on an install that predates it.
    """
    task = (BOOT_LINUX_VOICE_TASK if _task_registered(BOOT_LINUX_VOICE_TASK)
            else BOOT_LINUX_TASK)
    _run_later(delay, ["schtasks", "/run", "/tn", task])


def can_restart_into(os_name: str) -> str | None:
    """None if a reboot into `os_name` can be triggered, else why not."""
    if os_name == "windows":
        return can_restart_into_windows()
    if os_name == "linux":
        return can_restart_into_linux()
    return f"I don't know how to boot {os_name}"


def restart_into(os_name: str, delay: float = 6.0) -> None:
    """Reboot into `os_name` — check can_restart_into() first."""
    (restart_into_windows if os_name == "windows" else restart_into_linux)(delay)


# --- desktop notifications -------------------------------------------------

def notify(title: str, body: str = "", *, icon: str = "audio-input-microphone") -> None:
    """Show a desktop notification. Silent no-op where there is no notifier."""
    if IS_WINDOWS or not shutil.which("notify-send"):
        return
    try:
        _detached(["notify-send", "--app-name=JARVIS", f"--icon={icon}", title, body])
    except Exception:
        pass


# --- opening things --------------------------------------------------------

def open_target(target: str) -> None:
    """Open a file, folder, URL, or URI scheme with its default handler."""
    if hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
        return
    _detached(["xdg-open", target])


_LINUX_TERMINALS = (
    ("konsole", "--hold", "-e"),
    ("gnome-terminal", "--"),
    ("x-terminal-emulator", "-e"),
    ("xterm", "-hold", "-e"),
)


def open_terminal(argv: list[str], cwd: str | None = None) -> None:
    """Run `argv` in a new visible terminal window that stays open afterwards."""
    if IS_WINDOWS:
        subprocess.Popen(argv, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    for term in _LINUX_TERMINALS:
        if shutil.which(term[0]):
            _detached([*term, *argv], cwd=cwd)
            return
    # No terminal emulator: still run it, just without a window.
    _detached(argv, cwd=cwd)


# --- process trees ---------------------------------------------------------

def popen_group_kwargs() -> dict:
    """Popen kwargs that make the child a killable group leader (see kill_tree)."""
    return {} if IS_WINDOWS else {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Hard-kill a process and every descendant."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=_DEVNULL, stderr=_DEVNULL, timeout=5)
        return
    victims: list[int] = []
    try:
        import psutil
        parent = psutil.Process(pid)
        victims = [p.pid for p in parent.children(recursive=True)] + [pid]
    except Exception:
        victims = [pid]
    # The agent is spawned as a session leader, so killpg catches anything
    # psutil missed (children reparented after their own parent exited).
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for victim in victims:
        try:
            os.kill(victim, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# --- clipboard (Linux; Windows uses the Win32 API in tools/clipboard.py) ----

def _klipper(method: str, *args: str) -> str | None:
    tool = shutil.which("qdbus6") or shutil.which("qdbus")
    if not tool:
        return None
    try:
        res = subprocess.run(
            [tool, "org.kde.klipper", "/klipper",
             f"org.kde.klipper.klipper.{method}", *args],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    return res.stdout if res.returncode == 0 else None


def clipboard_get() -> str | None:
    """Clipboard text, "" when empty, or None when no backend is available."""
    if shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY"):
        try:
            res = subprocess.run(["wl-paste", "--no-newline", "--type", "text"],
                                 capture_output=True, text=True, timeout=3)
            # Non-zero just means "nothing (textual) to paste".
            return res.stdout if res.returncode == 0 else ""
        except Exception:
            pass
    out = _klipper("getClipboardContents")
    if out is not None:
        return out.removesuffix("\n")
    if shutil.which("xclip"):
        res = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                             capture_output=True, text=True, timeout=3)
        return res.stdout if res.returncode == 0 else ""
    return None


def clipboard_set(text: str) -> bool:
    if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
        try:
            # wl-copy forks a server that owns the selection; don't wait on it.
            proc = subprocess.Popen(["wl-copy", "--type", "text/plain"],
                                    stdin=subprocess.PIPE, stdout=_DEVNULL,
                                    stderr=_DEVNULL)
            proc.communicate(text.encode("utf-8"), timeout=3)
            if proc.returncode == 0:
                return True
        except Exception:
            pass
    if _klipper("setClipboardContents", text) is not None:
        return True
    if shutil.which("xclip"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), timeout=3, check=True)
            return True
        except Exception:
            pass
    return False


# --- screen capture --------------------------------------------------------

def grab_screen():
    """Screenshot of every monitor as a PIL image."""
    from PIL import Image, ImageGrab

    if IS_WINDOWS:
        return ImageGrab.grab(all_screens=True)

    # Wayland forbids direct framebuffer reads; go through the desktop's own
    # screenshot tool (Spectacle on KDE, grim on wlroots compositors).
    tmp = Path(tempfile.mkdtemp(prefix="friday-shot-")) / "screen.png"
    try:
        for argv in (
            ["spectacle", "--background", "--nonotify", "--fullscreen",
             "--output", str(tmp)],
            ["grim", str(tmp)],
        ):
            if not shutil.which(argv[0]):
                continue
            subprocess.run(argv, stdout=_DEVNULL, stderr=_DEVNULL, timeout=15)
            if tmp.exists() and tmp.stat().st_size > 0:
                with Image.open(tmp) as img:
                    return img.copy()
        return ImageGrab.grab()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
