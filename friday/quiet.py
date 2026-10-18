"""Quiet mode — when JARVIS must not make a sound.

Quiet means: the wake word and push-to-talk are ignored, nothing is spoken,
and JARVIS is reachable by text only (KRunner / `friday_ctl ask`).

It turns on by itself whenever the laptop is on a Wi-Fi network marked as
"school". A manual override (quiet on / quiet off) wins over that, but only
while the laptop stays on the network it was set on — so "let it talk" granted
in one room can't carry over into tomorrow's class, and a manual "quiet" at
home doesn't silently outlive the trip to somewhere else.

Deliberately free of any MCP/LiveKit import: the launcher and friday_ctl load
this module and must stay light.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parents[1] / "runtime" / "quiet.json"

# nmcli takes ~20ms (with --rescan no); the launcher asks on every wake-word hit.
_SSID_TTL = 15.0
_ssid_cache: tuple[float, str] = (0.0, "")


def current_ssid(*, fresh: bool = False) -> str:
    """Name of the connected Wi-Fi network, or "" (wired, offline, unknown)."""
    global _ssid_cache
    now = time.monotonic()
    if not fresh and now - _ssid_cache[0] < _SSID_TTL:
        return _ssid_cache[1]
    ssid = ""
    if shutil.which("nmcli"):
        try:
            # --rescan no: a plain `dev wifi` starts a fresh scan whenever the
            # last one is ~30 s old, which takes 3 s or more. That timed out, the
            # network read as "" for the next 15 s, and quiet mode lapsed at school.
            out = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi", "list", "--rescan", "no"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            for line in out.splitlines():
                active, _, name = line.partition(":")
                if active == "yes":
                    ssid = name.replace("\\:", ":")
                    break
        except Exception:
            ssid = ""
    _ssid_cache = (now, ssid)
    return ssid


def _load() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("school_ssids", [])
    data.setdefault("override", None)   # {"quiet": bool, "ssid": str} | None
    return data


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _live_override(data: dict, ssid: str) -> bool | None:
    """The manual override, if it was set on the network we're still on."""
    override = data.get("override")
    if isinstance(override, dict) and override.get("ssid") == ssid:
        return bool(override.get("quiet"))
    return None


def is_quiet() -> bool:
    data = _load()
    ssid = current_ssid()
    override = _live_override(data, ssid)
    if override is not None:
        return override
    return bool(ssid) and ssid in data["school_ssids"]


def status(*, fresh: bool = True) -> dict:
    """Everything a UI needs to explain why JARVIS is (not) quiet.

    `fresh=False` reuses the cached network name — for callers that poll.
    """
    data = _load()
    ssid = current_ssid(fresh=fresh)
    override = _live_override(data, ssid)
    at_school = bool(ssid) and ssid in data["school_ssids"]
    if override is not None:
        reason = "manual"
    elif at_school:
        reason = "school Wi-Fi"
    else:
        reason = "automatic"
    return {
        "quiet": override if override is not None else at_school,
        "reason": reason,
        "ssid": ssid,
        "at_school": at_school,
        "school_ssids": list(data["school_ssids"]),
    }


def set_override(quiet: bool | None) -> dict:
    """Force quiet on/off until the Wi-Fi network changes; None = automatic."""
    data = _load()
    if quiet is None:
        data["override"] = None
    else:
        data["override"] = {"quiet": bool(quiet), "ssid": current_ssid(fresh=True)}
    _save(data)
    return status()


def add_school(ssid: str | None = None) -> str:
    """Mark a network (default: the current one) as school. Returns its name."""
    ssid = (ssid or current_ssid(fresh=True)).strip()
    if not ssid:
        raise ValueError("not connected to Wi-Fi — pass the network name")
    data = _load()
    if ssid not in data["school_ssids"]:
        data["school_ssids"].append(ssid)
        _save(data)
    return ssid


def remove_school(ssid: str | None = None) -> str:
    ssid = (ssid or current_ssid(fresh=True)).strip()
    data = _load()
    if ssid in data["school_ssids"]:
        data["school_ssids"].remove(ssid)
        _save(data)
    return ssid
