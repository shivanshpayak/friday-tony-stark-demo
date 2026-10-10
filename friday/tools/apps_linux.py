"""
App launcher tools for Linux — open and close desktop apps.

Mirrors friday/tools/apps.py (the Windows implementation) with the same public
functions and MCP tool names, so the system prompt and the loop engine don't
care which one is loaded.

Resolution layers (first hit wins):
1. Pinned whitelist + aliases from config (canonical names like "vscode").
2. Auto-discovered .desktop entries (system, user, Flatpak, Snap).
3. Fuzzy fallback (normalized exact → token-AND → difflib close-match).

Launch runs the entry's Exec line detached. Close sends SIGTERM to the app's
processes — a polite quit, so apps can save state — and never touches the
desktop shell or JARVIS's own runtime.
"""

from __future__ import annotations

import configparser
import difflib
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from friday.config import APP_WHITELIST, APP_ALIASES, APP_DISCOVERED_ALIASES

log = logging.getLogger("friday-agent")

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _application_dirs() -> list[Path]:
    """Every directory that may hold .desktop entries, user-scoped first."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    roots = [data_home, *data_dirs,
             str(Path.home() / ".local/share/flatpak/exports/share"),
             "/var/lib/flatpak/exports/share",
             "/var/lib/snapd/desktop"]
    seen: list[Path] = []
    for root in roots:
        path = Path(root) / "applications"
        if root and path not in seen:
            seen.append(path)
    return seen


_NOISE_PREFIXES = ("uninstall ", "uninstall-", "help", "readme", "license", "release notes")

_DISCOVERED: dict[str, dict] = {}   # normalized_name -> {"desktop", "display", "exec", "process"}
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERED_READY = False

# Exec field codes (%f %U %i ...) are placeholders for files/URLs; drop them.
_FIELD_CODE = re.compile(r"%[a-zA-Z]")
# Wrappers whose own name says nothing about which app is running.
_WRAPPERS = {"env", "sh", "bash", "flatpak", "snap", "gtk-launch", "python3", "python"}


def _normalize(s: str) -> str:
    """Lowercase, drop version numbers and punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"\b\d+(?:\.\d+)*\b", " ", s)   # "PrusaSlicer 2.9.4" -> "prusaslicer"
    s = re.sub(r"[^\w\s]", " ", s)             # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _exec_argv(exec_line: str) -> list[str]:
    try:
        argv = shlex.split(_FIELD_CODE.sub("", exec_line))
    except ValueError:
        return []
    return [a for a in argv if a]


def _process_name(argv: list[str]) -> Optional[str]:
    """Best guess at the process name an Exec line ends up running as."""
    for arg in argv:
        if "=" in arg and not arg.startswith("/"):   # env VAR=value prefix
            continue
        base = os.path.basename(arg)
        if base in _WRAPPERS or arg.startswith("-") or arg == "run":
            continue
        return base
    return None


def _parse_desktop_file(path: Path) -> Optional[dict]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keys are case-sensitive
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except Exception:
        return None
    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "false").lower() == "true":
        return None
    if entry.get("Hidden", "false").lower() == "true":
        return None
    name, exec_line = entry.get("Name"), entry.get("Exec")
    if not name or not exec_line:
        return None
    argv = _exec_argv(exec_line)
    if not argv:
        return None
    return {
        "desktop": str(path),
        "display": name,
        "exec": argv,
        # A `--class=` argument survives into the real process's command line
        # even when Exec is a wrapper script. It is the only thing that tells
        # FRC VS Code apart from ordinary VS Code: both run a binary named
        # "code".
        "window_class": next((a for a in argv if a.startswith("--class=")), None),
        "terminal": entry.get("Terminal", "false").lower() == "true",
        "process": _process_name(argv),
    }


def _scan_desktop_entries() -> dict[str, dict]:
    """Walk every applications dir, return {normalized_name: entry}."""
    found: dict[str, dict] = {}
    for root in _application_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.desktop")):
            entry = _parse_desktop_file(path)
            if entry is None:
                continue
            if entry["display"].lower().startswith(_NOISE_PREFIXES):
                continue
            norm = _normalize(entry["display"])
            # First hit wins so user-scoped entries override system ones.
            if norm:
                found.setdefault(norm, entry)
    return found


def _ensure_discovered() -> dict[str, dict]:
    global _DISCOVERED_READY
    if _DISCOVERED_READY:
        return _DISCOVERED
    with _DISCOVERY_LOCK:
        if _DISCOVERED_READY:
            return _DISCOVERED
        _DISCOVERED.clear()
        _DISCOVERED.update(_scan_desktop_entries())
        _DISCOVERED_READY = True
        log.info("apps: discovered %d apps (.desktop entries)", len(_DISCOVERED))
    return _DISCOVERED


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _resolve(name: str) -> Optional[tuple[str, dict, str]]:
    """Resolve a name to (display_name, entry, source).

    `source` is "pinned" or "discovered".
    Entry shape:
      pinned:     {"launch": tuple[str, ...], "process": str}
      discovered: {"desktop": str, "display": str, "exec": list, "process": str | None}
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None

    # 0. Spoken alias for a discovered app ("wpilib" -> FRC VS Code). First,
    #    because every later layer has a plausible wrong answer for these.
    target = APP_DISCOVERED_ALIASES.get(_normalize(needle))
    if target:
        entry = _ensure_discovered().get(target)
        if entry is not None:
            return entry["display"], entry, "discovered"

    # 1. Pinned: exact key
    if needle in APP_WHITELIST:
        return needle, APP_WHITELIST[needle], "pinned"
    # 2. Pinned: alias
    if needle in APP_ALIASES and APP_ALIASES[needle] in APP_WHITELIST:
        canon = APP_ALIASES[needle]
        return canon, APP_WHITELIST[canon], "pinned"

    # 3. Discovered: normalized exact. Checked before the pinned substring
    #    match so "claude code" or "frc vs code" reach their own entries.
    disc = _ensure_discovered()
    norm = _normalize(needle)
    if norm and norm in disc:
        e = disc[norm]
        return e["display"], e, "discovered"

    # 4. Pinned: substring (e.g. "spotify app" -> "spotify")
    for key, entry in APP_WHITELIST.items():
        if needle in key or key in needle:
            return key, entry, "pinned"

    if not norm:
        return None

    # 5. Discovered: token-AND match (all needle tokens present in candidate)
    needle_tokens = set(norm.split())
    if needle_tokens:
        cands = [
            (k, v) for k, v in disc.items()
            if needle_tokens.issubset(set(k.split()))
        ]
        if cands:
            # Prefer the shortest (most specific) match.
            cands.sort(key=lambda kv: len(kv[0]))
            k, v = cands[0]
            return v["display"], v, "discovered"

    # 6. Fuzzy difflib fallback
    close = difflib.get_close_matches(norm, list(disc.keys()), n=1, cutoff=0.75)
    if close:
        v = disc[close[0]]
        return v["display"], v, "discovered"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _spawn(argv: list[str]) -> None:
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        cwd=str(Path.home()),
    )


def _pinned_command(entry: dict) -> Optional[str]:
    launch = entry.get("launch") or ()
    if isinstance(launch, str):
        launch = (launch,)
    for candidate in launch:
        if shutil.which(candidate):
            return candidate
    return None


def _discovered_for_pinned(entry: dict) -> Optional[dict]:
    """The .desktop entry backing a pinned app (covers Snap/Flatpak installs
    whose command isn't the one listed in the whitelist)."""
    wanted = {str(c).lower() for c in (entry.get("launch") or ())}
    wanted.add(str(entry.get("process", "")).lower())
    for disc_entry in _ensure_discovered().values():
        if (disc_entry.get("process") or "").lower() in wanted:
            return disc_entry
    return None


def is_running(name: str) -> bool:
    """True if the app `name` resolves to has a live process."""
    resolved = _resolve(name)
    if resolved is None:
        return False
    _display, entry, _source = resolved
    process = str(entry.get("process") or "").lower()
    if not process:
        return False
    try:
        return bool(_matching_processes({process, process[:15]},
                                        entry.get("window_class")))
    except Exception:
        return False


def launch_app_with(name: str, *args: str) -> str:
    """Open a discovered app with extra arguments (e.g. a folder to open)."""
    resolved = _resolve(name)
    if resolved is None or resolved[2] != "discovered":
        return launch_app(name)
    display, entry, _source = resolved
    try:
        _spawn([*entry["exec"], *args])
        return f"Opening {display}."
    except Exception as e:
        return f"Couldn't open {display}: {e}"


def launch_app(name: str) -> str:
    """Open a desktop app by name. Returns a short voice-friendly result."""
    resolved = _resolve(name)
    if resolved is None:
        return f"I don't see an app called {name} on this machine."
    display, entry, source = resolved
    try:
        if source == "pinned":
            command = _pinned_command(entry)
            if command:
                _spawn([command])
                return f"Opening {display}."
            entry = _discovered_for_pinned(entry)
            if entry is None:
                return f"{display} doesn't appear to be installed, sir."
        if entry.get("terminal"):
            from friday import plat
            plat.open_terminal(entry["exec"])
        else:
            _spawn(entry["exec"])
        return f"Opening {display}."
    except Exception as e:
        return f"Couldn't open {display}: {e}"


# ---------------------------------------------------------------------------
# Processes close_app must never kill
# ---------------------------------------------------------------------------
# The desktop shell / compositor / session plumbing, and JARVIS's own runtime.
_UNKILLABLE_PROCESSES = frozenset({
    "python", "python3", "python3.11", "uv",
    "plasmashell", "kwin_wayland", "kwin_x11", "ksmserver", "kded6", "kded5",
    "gnome-shell", "xwayland", "systemd", "dbus-daemon", "dbus-broker",
    "pipewire", "pipewire-pulse", "wireplumber", "sddm", "bash", "sh", "zsh",
})


def _matching_processes(names: set[str], window_class: Optional[str] = None,
                        not_classed: bool = False) -> list:
    """Our own processes matching `names`, or — when `window_class` is given —
    the ones launched with that `--class=` argument. `not_classed` drops
    processes that carry any `--class=`: they belong to a differently-branded
    build of the same binary (plain VS Code must not close FRC VS Code)."""
    import psutil

    me = os.getuid()
    matches = []
    classed_binaries: set = set()
    for proc in psutil.process_iter(["name", "exe", "uids", "cmdline"]):
        try:
            info = proc.info
            if info["uids"] is None or info["uids"].real != me:
                continue
            pname = (info["name"] or "").lower()
            exe = os.path.basename(info["exe"] or "").lower()
            if pname in _UNKILLABLE_PROCESSES or exe in _UNKILLABLE_PROCESSES:
                continue
            cmdline = info["cmdline"] or []
            if window_class:
                if window_class in cmdline:
                    matches.append(proc)
                continue
            if pname in names or exe in names:
                if any(a.startswith("--class=") for a in cmdline):
                    classed_binaries.add(info["exe"])
                    if not_classed:
                        continue
                matches.append(proc)
        except Exception:
            continue
    if not_classed and classed_binaries:
        # The branded build's helper processes (renderer, GPU...) carry no
        # --class themselves, but they run the same binary as their parent.
        matches = [p for p in matches if p.info["exe"] not in classed_binaries]
    return matches


def _app_scope(pid: int) -> Optional[str]:
    """The systemd user scope a process was launched in, e.g.
    'snap.spotify.spotify-<uuid>.scope' or 'app-org.kde.kate-1234.scope'."""
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    unit = cgroup.strip().rsplit("/", 1)[-1]
    if unit.endswith(".scope") and unit.startswith(("snap.", "app-")):
        return unit
    return None


def _stop_scopes(procs) -> bool:
    """Ask systemd to stop the scopes of processes we couldn't signal ourselves.

    Snap-confined apps (Spotify) only accept signals from *unconfined* senders.
    When JARVIS itself runs under a named AppArmor profile — started from
    another confined app's terminal, say — its SIGTERM is refused. The user's
    systemd manager is always unconfined, so it can deliver the stop for us.
    """
    units = {unit for unit in (_app_scope(p.pid) for p in procs) if unit}
    stopped = False
    for unit in units:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "stop", "--no-block", unit],
                capture_output=True, text=True, timeout=5,
            )
            stopped = stopped or res.returncode == 0
        except Exception as e:
            log.debug("systemctl stop %s failed: %s", unit, e)
    return stopped


def close_app(name: str) -> str:
    """Close a running desktop app by name (SIGTERM). Refuses to touch the
    desktop shell or JARVIS's own runtime."""
    resolved = _resolve(name)
    if resolved is None:
        return f"I don't see an app called {name} on this machine."
    display, entry, source = resolved

    names: set[str] = set()
    if entry.get("process"):
        names.add(str(entry["process"]).lower())
    if source == "pinned":
        names.update(str(c).lower() for c in (entry.get("launch") or ()))
        backing = _discovered_for_pinned(entry)
        if backing and backing.get("process"):
            names.add(backing["process"].lower())
    # Kernel process names are capped at 15 chars; match the truncated form too.
    names |= {n[:15] for n in names}
    names -= {""}

    if names and names <= _UNKILLABLE_PROCESSES:
        return f"I can't close {display} — I'm running on it myself."
    names -= _UNKILLABLE_PROCESSES
    if not names:
        return f"I don't know which process {display} runs as, sir."

    try:
        window_class = entry.get("window_class")
        procs = _matching_processes(names, window_class,
                                    not_classed=not window_class)
        if not procs:
            return f"{display} wasn't running."
        signalled, refused = 0, []
        for proc in procs:
            try:
                proc.terminate()
                signalled += 1
            except Exception:
                refused.append(proc)
        if refused and _stop_scopes(refused):
            signalled += 1
        if not signalled:
            return f"I wasn't permitted to close {display}, sir."
        return f"Closed {display}."
    except Exception as e:
        return f"Couldn't close {display}: {e}"


def rescan_apps() -> str:
    """Clear the discovery cache and rescan .desktop entries. Use after
    installing something new without restarting Jarvis."""
    global _DISCOVERED_READY
    with _DISCOVERY_LOCK:
        _DISCOVERED.clear()
        _DISCOVERED_READY = False
    fresh = _ensure_discovered()
    return f"Rescanned. I see {len(fresh)} apps now."


def list_known_apps(limit: int = 0) -> str:
    """Return a comma-separated list of known apps (pinned + discovered)."""
    disc = _ensure_discovered()
    names = sorted(set(APP_WHITELIST.keys()) | {v["display"] for v in disc.values()})
    if limit and len(names) > limit:
        names = names[:limit] + [f"... and {len(names) - limit} more"]
    return ", ".join(names)


def register(mcp):
    """Register app launcher tools onto the FastMCP server."""

    # MCP tool names are the LLM-facing names — keep them aligned with the
    # system prompt in friday/config.py and with friday/tools/apps.py.

    @mcp.tool(name="launch_app")
    def _mcp_launch_app(name: str) -> str:
        """Open a desktop app by name. Use this when the user asks to open,
        launch, or start an app (e.g. "open Spotify", "launch VS Code",
        "start Chrome"). Pass the app name as the user said it; common
        aliases like "vs code", "browser", "file explorer" are accepted."""
        return launch_app(name)

    @mcp.tool(name="close_app")
    def _mcp_close_app(name: str) -> str:
        """Close a running desktop app by name. Use when the user asks to close,
        quit, or kill an app (e.g. "close Chrome", "quit Spotify"). Same
        name handling as launch_app."""
        return close_app(name)

    @mcp.tool(name="rescan_apps")
    def _mcp_rescan_apps() -> str:
        """Rebuild the list of installed apps. Use when the user says they just
        installed something and Jarvis can't find it, or asks to
        "refresh apps" / "rescan apps"."""
        return rescan_apps()


# Pre-warm the discovery cache in the background when the module loads.
threading.Thread(target=_ensure_discovered, daemon=True).start()
