"""The phone: send it links, files and notes through KDE Connect, and find it
through Find My.

Talks to kdeconnectd over the session bus with busctl, the way media_control
talks to MPRIS players. The phone is the paired device KDE Connect calls a
"phone" (PHONE_NAME in config picks one by name if there are several).
Replies say "your phone", never the device's name: that's usually built from
the user's own name, which JARVIS doesn't say.

An iPhone drops off a minute or two after the KDE Connect app leaves its
screen: iOS suspends it, and nothing from this side wakes it back up. So send()
checks the phone is reachable first and says how to fix it if not. For the
same reason a lost phone is out of KDE Connect's reach, so finding it doesn't
use KDE Connect at all: find_my_phone() opens PHONE_FIND_URL (iCloud Find My),
whose Play Sound reaches the phone wherever it is.

Deliberately free of any MCP import: friday/tools/phone.py wraps these as tools.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from friday import plat
from friday.config import FRIDAY_FILE_ROOTS, PHONE_FIND_URL, PHONE_NAME

_SERVICE = "org.kde.kdeconnect"
_ROOT = "/modules/kdeconnect"
_DAEMON = "org.kde.kdeconnect.daemon"
_DEVICE = "org.kde.kdeconnect.device"
_ERRORS = (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError)

NOT_RUNNING = "KDE Connect isn't running on this machine, sir."
NOT_PAIRED = "There's no phone paired with KDE Connect, sir."
OFFLINE = "Your phone isn't connected, sir. Opening KDE Connect on it should bring it back."


@dataclass(frozen=True)
class Phone:
    id: str
    name: str
    reachable: bool

    @property
    def path(self) -> str:
        return f"{_ROOT}/devices/{self.id}"


def _run(argv: list[str]) -> str:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or f"{argv[0]} failed")
    return res.stdout


def _busctl(*args: str):
    """One busctl call; its first return value (None for a void method)."""
    out = _run(["busctl", "--user", "--json=short", *args]).strip()
    if not out:
        return None
    data = json.loads(out)["data"]
    # `call` wraps its return values in a list.
    return data[0] if isinstance(data, list) else data


def _call(path: str, interface: str, method: str, signature: str = "", *values: str):
    args = ["call", _SERVICE, path, interface, method]
    if signature:
        args += [signature, *values]
    return _busctl(*args)


def device() -> Phone | str:
    """The phone to talk to, or the reason there isn't one."""
    if plat.IS_WINDOWS:
        return "Sending to the phone works on Linux only, sir."
    try:
        ids = _call(_ROOT, _DAEMON, "devices", "bb", "false", "true")  # paired, reachable or not
    except _ERRORS:
        return NOT_RUNNING
    found = []
    for device_id in ids or []:
        try:
            props = _call(f"{_ROOT}/devices/{device_id}",
                          "org.freedesktop.DBus.Properties", "GetAll", "s", _DEVICE)
        except _ERRORS:
            continue

        def value(key, default):
            return (props.get(key) or {}).get("data", default)

        found.append((value("type", ""),
                      Phone(device_id, value("name", ""), bool(value("isReachable", False)))))
    if PHONE_NAME:
        found = [(kind, p) for kind, p in found if PHONE_NAME.lower() in p.name.lower()]
    if not found:
        return NOT_PAIRED
    # Phones before tablets and laptops, then whichever is connected.
    found.sort(key=lambda kp: (kp[0] != "phone", not kp[1].reachable))
    return found[0][1]


def _connected() -> Phone | str:
    phone = device()
    if isinstance(phone, Phone) and not phone.reachable:
        return OFFLINE
    return phone


def find_my_phone() -> str:
    plat.open_target(PHONE_FIND_URL)
    return "Find My is on the screen, sir. Play Sound will ring it."


def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", "~/", "file://"))


def _in_roots(path: Path) -> bool:
    return any(path == root or root in path.parents for root in FRIDAY_FILE_ROOTS)


def send(content: str) -> str:
    """A link opens on the phone, a file is saved to it, anything else arrives
    as a notification."""
    content = (content or "").strip()
    if not content:
        return "Send what, sir?"
    path = None
    if _looks_like_path(content):
        path = Path(content.removeprefix("file://")).expanduser().resolve()
        if not path.is_file():
            return "I can't find that file, sir."
        if not _in_roots(path):
            return "That file is outside the folders I'm allowed to touch, sir."
    phone = _connected()
    if isinstance(phone, str):
        return phone
    try:
        if path:
            _call(f"{phone.path}/share", f"{_DEVICE}.share", "shareUrl", "s", path.as_uri())
            return f"Sending {path.name} to your phone, sir."
        if content.startswith(("http://", "https://")):
            _call(f"{phone.path}/share", f"{_DEVICE}.share", "shareUrl", "s", content)
            return "Link sent to your phone, sir."
        _call(f"{phone.path}/ping", f"{_DEVICE}.ping", "sendPing", "s", content)
        return "Sent to your phone, sir."
    except _ERRORS as e:
        return f"KDE Connect wouldn't send it, sir: {e}"
