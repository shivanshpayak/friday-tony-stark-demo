"""
App launcher tools — open and close desktop apps.

Resolution layers (first hit wins):
1. Pinned whitelist + aliases from config (canonical names like "vscode").
2. Auto-discovered Start Menu shortcuts (everything you've installed).
3. Fuzzy fallback (normalized exact → token-AND → difflib close-match).

Launch uses os.startfile for .lnk targets and `cmd /c start` for pinned
launch commands (Windows App Paths). Close uses `taskkill /F /IM <exe>` —
exe names for discovered apps come from lazy .lnk target resolution.
"""

from __future__ import annotations

import difflib
import glob
import logging
import os
import re
import subprocess
import threading
from typing import Optional

from friday.config import APP_WHITELIST, APP_ALIASES

log = logging.getLogger("friday-agent")

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_START_MENU_ROOTS = [
    os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
    os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
]

# Shortcut basenames we never want to surface (uninstallers, docs, etc.)
_NOISE_PREFIXES = ("uninstall ", "uninstall-", "help", "readme", "license", "release notes")

_DISCOVERED: dict[str, dict] = {}   # normalized_name -> {"lnk", "display", "process"}
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERED_READY = False


def _normalize(s: str) -> str:
    """Lowercase, drop version numbers and punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"\b\d+(?:\.\d+)*\b", " ", s)   # "PrusaSlicer 2.9.4" -> "prusaslicer"
    s = re.sub(r"[^\w\s]", " ", s)             # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _scan_start_menu() -> dict[str, dict]:
    """Walk both Start Menu roots, return {normalized_name: entry}."""
    found: dict[str, dict] = {}
    for root in _START_MENU_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "**", "*.lnk")
        for path in glob.iglob(pattern, recursive=True):
            base = os.path.basename(path)[:-4]  # strip .lnk
            low = base.lower()
            if any(low.startswith(p) for p in _NOISE_PREFIXES):
                continue
            norm = _normalize(base)
            if not norm:
                continue
            # First hit wins so user-scoped apps (APPDATA, listed first) take
            # precedence over system-scoped duplicates.
            found.setdefault(norm, {"lnk": path, "display": base, "process": None})
    return found


def _scan_store_apps() -> dict[str, dict]:
    """Use Get-StartApps to surface Microsoft Store / UWP apps that don't have
    .lnk files (e.g. Minecraft Launcher). One-time ~500ms PowerShell cost."""
    out: dict[str, dict] = {}
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ForEach-Object { \"$($_.Name)|$($_.AppID)\" }"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            name, _, app_id = line.partition("|")
            name = name.strip()
            app_id = app_id.strip()
            if not name or not app_id:
                continue
            low = name.lower()
            if any(low.startswith(p) for p in _NOISE_PREFIXES):
                continue
            norm = _normalize(name)
            if norm:
                out.setdefault(norm, {"app_id": app_id, "display": name, "process": None})
    except Exception as e:
        log.debug("Get-StartApps scan failed: %s", e)
    return out


def _ensure_discovered() -> dict[str, dict]:
    global _DISCOVERED_READY
    if _DISCOVERED_READY:
        return _DISCOVERED
    with _DISCOVERY_LOCK:
        if _DISCOVERED_READY:
            return _DISCOVERED
        _DISCOVERED.clear()
        # Start Menu first so .lnk-backed apps (with resolvable exe targets
        # for taskkill) win over Store/UWP duplicates of the same name.
        _DISCOVERED.update(_scan_start_menu())
        for k, v in _scan_store_apps().items():
            _DISCOVERED.setdefault(k, v)
        _DISCOVERED_READY = True
        log.info("apps: discovered %d apps (start-menu + store)", len(_DISCOVERED))
    return _DISCOVERED


def _resolve_lnk_process(lnk_path: str) -> Optional[str]:
    """Resolve a .lnk's TargetPath to an exe basename for taskkill. ~5-20ms."""
    try:
        import win32com.client  # type: ignore
        shell = win32com.client.Dispatch("WScript.Shell")
        target = shell.CreateShortcut(lnk_path).TargetPath
        if target and target.lower().endswith(".exe"):
            return os.path.basename(target)
    except Exception as e:
        log.debug("lnk target resolution failed for %s: %s", lnk_path, e)
    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _resolve(name: str) -> Optional[tuple[str, dict, str]]:
    """Resolve a name to (display_name, entry, source).

    `source` is "pinned" or "discovered" — useful for choosing launch path.
    Entry shape:
      pinned:     {"launch": str, "process": str}
      discovered: {"lnk": str, "display": str, "process": str | None}
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None

    # 1. Pinned: exact key
    if needle in APP_WHITELIST:
        return needle, APP_WHITELIST[needle], "pinned"
    # 2. Pinned: alias
    if needle in APP_ALIASES:
        canon = APP_ALIASES[needle]
        return canon, APP_WHITELIST[canon], "pinned"
    # 3. Pinned: substring (e.g. "spotify app" -> "spotify")
    for key, entry in APP_WHITELIST.items():
        if needle == key or needle in key or key in needle:
            return key, entry, "pinned"

    # 4. Discovered: normalized exact
    disc = _ensure_discovered()
    norm = _normalize(needle)
    if not norm:
        return None
    if norm in disc:
        e = disc[norm]
        return e["display"], e, "discovered"

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

def _discovery_entry_for(display: str) -> Optional[dict]:
    """Return the discovery cache entry for this display name (or None).
    Used to upgrade pinned launches to the real Start Menu .lnk or UWP
    AppsFolder path — pinned `launch` strings rely on App Paths / PATH,
    which some per-user installs (Discord, Spotify UWP, Obsidian) never
    register in."""
    disc = _ensure_discovered()
    norm = _normalize(display)
    return disc.get(norm) if norm else None


def launch_app(name: str) -> str:
    """Open a desktop app by name. Returns a short voice-friendly result."""
    resolved = _resolve(name)
    if resolved is None:
        return f"I don't see an app called {name} on this machine."
    display, entry, source = resolved
    try:
        # Prefer the Start Menu .lnk or Store AppsFolder path whenever
        # discovery has one — both are more reliable than
        # `cmd /c start "" <name>`, which depends on App Paths / PATH.
        # Falls back to the pinned launch command as a last resort.
        disc_entry = entry if source == "discovered" else _discovery_entry_for(display)
        if disc_entry and "lnk" in disc_entry:
            os.startfile(disc_entry["lnk"])  # type: ignore[attr-defined]
        elif disc_entry and "app_id" in disc_entry:
            os.startfile(f"shell:AppsFolder\\{disc_entry['app_id']}")  # type: ignore[attr-defined]
        elif "launch" in entry:
            subprocess.Popen(
                ["cmd", "/c", "start", "", entry["launch"]],
                shell=False,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True,
            )
        else:
            return f"Couldn't open {display}: no launch path."
        return f"Opening {display}."
    except Exception as e:
        return f"Couldn't open {display}: {e}"


def close_app(name: str) -> str:
    """Close a running desktop app by name. Uses taskkill /F."""
    resolved = _resolve(name)
    if resolved is None:
        return f"I don't see an app called {name} on this machine."
    display, entry, source = resolved

    process = entry.get("process")
    if not process and source == "discovered" and "lnk" in entry:
        process = _resolve_lnk_process(entry["lnk"])
        entry["process"] = process  # cache for next time

    # We might still not have a process (e.g. UWP apps without .lnk).
    # We will try taskkill first if we have a process, then fallback to PowerShell.

    if process:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", process],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return f"Closed {display}."
            stderr = (result.stderr or "").strip().lower()
            if "not found" in stderr or "no running" in stderr or result.returncode == 128:
                return f"{display} wasn't running."
            return (
                f"Couldn't close {display}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        except subprocess.TimeoutExpired:
            return f"Closing {display} timed out."
        except Exception as e:
            return f"Couldn't close {display}: {e}"
            
    # Fallback for unknown processes (like UWP Store apps without .lnk)
    # We use PowerShell to find the process by its MainWindowTitle or exact ProcessName match.
    escaped_display = display.replace("'", "''")
    norm_disp = _normalize(display).replace("'", "''")
    
    script = (
        f"Get-Process -ErrorAction SilentlyContinue | Where-Object {{ "
        f"$_.MainWindowTitle -match '{escaped_display}' -or "
        f"($_.ProcessName -match '{norm_disp}' -and $_.ProcessName -notmatch 'explorer|svchost') "
        f"}} | Stop-Process -Force"
    )
    
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode == 0:
            return f"Closed {display}."
        return f"{display} wasn't running or couldn't be closed."
    except Exception as e:
        return f"Couldn't close {display} via fallback: {e}"


def rescan_apps() -> str:
    """Clear the discovery cache and rescan Start Menu. Use after installing
    something new without restarting Jarvis."""
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
    # system prompt in friday/config.py. The underlying Python functions
    # (launch_app, close_app, rescan_apps) remain importable for other
    # internal callers.

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

# Pre-warm the discovery cache in the background when the module loads,
# so that the 3-5 second PowerShell scan is finished before the user
# ever asks to open an app.
threading.Thread(target=_ensure_discovered, daemon=True).start()
