"""Curated, safe action + check builders for the loop-until-success engine.

Everything the loop can DO or CHECK must be registered here — no arbitrary shell,
no GUI. Each builder validates its args and returns a zero-arg callable that the
runner invokes each iteration.
"""
from __future__ import annotations

import socket
import subprocess
from typing import Any, Callable, Dict


# --------------------------------------------------------------------------- #
# low-level helpers
# --------------------------------------------------------------------------- #
def _netsh(*args: str, timeout: float = 10.0) -> str:
    proc = subprocess.run(
        ["netsh", *args], capture_output=True, text=True, timeout=timeout
    )
    return (proc.stdout or proc.stderr or "").strip()


def _current_or_first_ssid() -> str:
    """Best-effort SSID: the interface's current SSID, else the first saved
    wlan profile. Returns '' if neither is available."""
    iface = _netsh("wlan", "show", "interfaces")
    for line in iface.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("ssid") and ":" in s and "bssid" not in low:
            val = s.split(":", 1)[1].strip()
            if val:
                return val
    profiles = _netsh("wlan", "show", "profiles")
    for line in profiles.splitlines():
        if "All User Profile" in line and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# call_tool safelist (non-destructive existing tools only)
# --------------------------------------------------------------------------- #
_CALL_TOOL_SAFELIST: Dict[str, Callable[..., Any]] = {}


def _load_safelist() -> None:
    if _CALL_TOOL_SAFELIST:
        return
    from friday.tools.apps import launch_app
    _CALL_TOOL_SAFELIST["launch_app"] = launch_app


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def _action_wifi_connect(args: Dict[str, Any]) -> Callable[[], str]:
    ssid = str(args.get("ssid", "")).strip()
    if not ssid:
        raise ValueError("wifi_connect requires an 'ssid' arg.")
    return lambda: _netsh("wlan", "connect", f"name={ssid}")


def _action_wifi_reconnect(args: Dict[str, Any]) -> Callable[[], str]:
    requested = str(args.get("ssid", "")).strip()

    def run() -> str:
        ssid = requested or _current_or_first_ssid()
        _netsh("wlan", "disconnect")
        if not ssid:
            return "No known Wi-Fi profile to reconnect to."
        return _netsh("wlan", "connect", f"name={ssid}")

    return run


def _action_call_tool(args: Dict[str, Any]) -> Callable[[], str]:
    tool = str(args.get("tool", "")).strip()
    if tool not in _CALL_TOOL_SAFELIST:
        raise ValueError(
            f"call_tool '{tool}' is not allowed. Allowed: "
            f"{', '.join(sorted(_CALL_TOOL_SAFELIST)) or '(none)'}."
        )
    fn = _CALL_TOOL_SAFELIST[tool]
    tool_args = {k: v for k, v in args.items() if k != "tool"}
    return lambda: str(fn(**tool_args))


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def _check_online(args: Dict[str, Any]) -> Callable[[], bool]:
    host = str(args.get("host", "1.1.1.1"))
    port = int(args.get("port", 53))
    return lambda: _tcp_reachable(host, port)


def _check_host_reachable(args: Dict[str, Any]) -> Callable[[], bool]:
    host = str(args.get("host", "")).strip()
    if not host:
        raise ValueError("host_reachable requires a 'host' arg.")
    port = int(args.get("port", 80))
    return lambda: _tcp_reachable(host, port)


def _check_wifi_connected(args: Dict[str, Any]) -> Callable[[], bool]:
    want = str(args.get("ssid", "")).strip()

    def check() -> bool:
        text = _netsh("wlan", "show", "interfaces")
        connected = any(
            line.strip().lower().startswith("state") and "connected" in line.lower()
            and "disconnected" not in line.lower()
            for line in text.splitlines()
        )
        if not connected:
            return False
        return (want.lower() in text.lower()) if want else True

    return check


def _check_process_running(args: Dict[str, Any]) -> Callable[[], bool]:
    name = str(args.get("name", "")).strip().lower()
    if not name:
        raise ValueError("process_running requires a 'name' arg.")

    def check() -> bool:
        import psutil
        for proc in psutil.process_iter(["name"]):
            pname = (proc.info.get("name") or "").lower()
            if name == pname or name == pname.removesuffix(".exe"):
                return True
        return False

    return check


# --------------------------------------------------------------------------- #
# registries + public API
# --------------------------------------------------------------------------- #
_ACTIONS: Dict[str, Callable[[Dict[str, Any]], Callable[[], str]]] = {
    "wifi_connect": _action_wifi_connect,
    "wifi_reconnect": _action_wifi_reconnect,
    "call_tool": _action_call_tool,
}
_CHECKS: Dict[str, Callable[[Dict[str, Any]], Callable[[], bool]]] = {
    "online": _check_online,
    "wifi_connected": _check_wifi_connected,
    "host_reachable": _check_host_reachable,
    "process_running": _check_process_running,
}


def build_action(name: str, args: Dict[str, Any]) -> Callable[[], str]:
    _load_safelist()
    if name not in _ACTIONS:
        raise ValueError(
            f"Unknown action '{name}'. Valid actions: {', '.join(sorted(_ACTIONS))}."
        )
    return _ACTIONS[name](args or {})


def build_check(name: str, args: Dict[str, Any]) -> Callable[[], bool]:
    if name not in _CHECKS:
        raise ValueError(
            f"Unknown check '{name}'. Valid checks: {', '.join(sorted(_CHECKS))}."
        )
    return _CHECKS[name](args or {})


def capabilities() -> str:
    return (
        "Actions: wifi_connect(ssid), wifi_reconnect(ssid optional), "
        "call_tool(tool, ...) [tool one of: launch_app]. "
        "Checks: online(host optional, port optional), wifi_connected(ssid optional), "
        "host_reachable(host, port optional), process_running(name)."
    )
