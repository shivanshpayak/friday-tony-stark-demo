"""
JARVIS in KRunner — type to JARVIS from Alt+Space.

    j what's the battery at
    jarvis open youtube

Pick the "Ask JARVIS" result; the reply arrives as a desktop notification.
Nothing is ever spoken, so this is how JARVIS is used in quiet mode.

KRunner talks to runners over D-Bus (org.kde.krunner1). This file is that
D-Bus service; the session bus starts it on demand. It only forwards text to
the running launcher's control socket (see friday_ctl.request) — it holds no
model, no audio, no state.

Install / remove (writes two small files under ~/.local/share):
    uv run python friday_krunner.py --install
    uv run python friday_krunner.py --uninstall
"""

import re
import subprocess
import sys
import threading
from pathlib import Path

BUS_NAME = "com.friday.jarvis"
OBJECT_PATH = "/runner"
RUNNER_IFACE = "org.kde.krunner1"

# "j <text>" or "jarvis <text>", optionally "jarvis, <text>".
_QUERY = re.compile(r"^\s*(?:j|jarvis)[\s,:]+(\S.*)$", re.IGNORECASE)
_ICON = "audio-input-microphone"
_EXACT_MATCH = 100          # KRunner::QueryMatch::ExactMatch
_IDLE_EXIT_SECONDS = 1800   # the bus restarts us on the next query

_HOME = Path.home()
PLUGIN_FILE = _HOME / ".local/share/krunner/dbusplugins/jarvis.desktop"
SERVICE_FILE = _HOME / ".local/share/dbus-1/services" / f"{BUS_NAME}.service"


def matches_for(query: str) -> list[tuple]:
    """KRunner match tuples a(sssida{sv}) for a query; [] if it isn't for us."""
    found = _QUERY.match(query or "")
    if not found:
        return []
    text = found.group(1).strip()
    # Run() only receives the match id, so the request rides inside it. The
    # model is NOT called here — Match fires on every keystroke.
    return [(
        "ask:" + text,
        f"Ask JARVIS: {text}",
        _ICON,
        _EXACT_MATCH,
        1.0,
        {"subtext": ("s", "Reply arrives as a notification — never spoken")},
    )]


def _answer(text: str) -> None:
    """Forward one request to the launcher and show the reply."""
    from friday import plat
    from friday_ctl import ask

    title = text if len(text) <= 60 else text[:57] + "…"
    reply, error = ask(text)
    if reply:
        plat.notify(title, reply)
    else:
        plat.notify("JARVIS couldn't answer", error or "No reply.",
                    icon="dialog-warning")


def run_match(match_id: str) -> None:
    if match_id.startswith("ask:"):
        # Answering takes seconds; KRunner must get its Run() reply at once.
        threading.Thread(target=_answer, args=(match_id[4:],), daemon=True).start()


def serve() -> None:
    from jeepney import HeaderFields, MessageType, new_error, new_method_return
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    # Plain send, not send_and_get_reply: when the bus starts us for a query,
    # that query is delivered right behind the RequestName reply, and waiting
    # for the reply here would read and discard it (KRunner's first search
    # after login then timed out). The loop below sees both.
    conn.send(message_bus.RequestName(BUS_NAME))

    while True:
        try:
            msg = conn.receive(timeout=_IDLE_EXIT_SECONDS)
        except TimeoutError:
            return
        if msg.header.message_type != MessageType.method_call:
            continue
        fields = msg.header.fields
        member = fields.get(HeaderFields.member)
        interface = fields.get(HeaderFields.interface)

        if interface == RUNNER_IFACE and member == "Match":
            reply = new_method_return(msg, "a(sssida{sv})", (matches_for(msg.body[0]),))
        elif interface == RUNNER_IFACE and member == "Actions":
            reply = new_method_return(msg, "a(sss)", ([],))
        elif interface == RUNNER_IFACE and member == "Run":
            run_match(msg.body[0])
            reply = new_method_return(msg)
        elif interface == RUNNER_IFACE and member in ("Teardown", "Config"):
            reply = new_method_return(msg)
        elif interface == "org.freedesktop.DBus.Peer" and member == "Ping":
            reply = new_method_return(msg)
        else:
            reply = new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod",
                              "s", (f"{interface}.{member}",))
        conn.send(reply)


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

def _reload_desktop() -> None:
    """Make the session bus and KRunner notice the files right now."""
    for argv in (
        ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
         "org.freedesktop.DBus", "ReloadConfig"],
        ["kquitapp6", "krunner"],   # it relaunches on the next Alt+Space
    ):
        try:
            subprocess.run(argv, capture_output=True, timeout=10)
        except Exception:
            pass


def install() -> None:
    script = Path(__file__).resolve()
    PLUGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_FILE.write_text(
        "[Desktop Entry]\n"
        "Name=JARVIS\n"
        "Comment=Type to JARVIS: j <request>\n"
        "Icon=" + _ICON + "\n"
        "Type=Service\n"
        "X-KDE-PluginInfo-Name=jarvis\n"
        "X-KDE-PluginInfo-EnabledByDefault=true\n"
        "X-Plasma-API=DBus\n"
        f"X-Plasma-DBusRunner-Service={BUS_NAME}\n"
        f"X-Plasma-DBusRunner-Path={OBJECT_PATH}\n"
        "X-Plasma-Request-Actions-Once=true\n"
        "X-Plasma-Runner-Min-Letter-Count=3\n"
        "X-Plasma-Runner-Match-Regex=^\\\\s*(j|jarvis)[\\\\s,:]\n",
        encoding="utf-8",
    )
    SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(
        "[D-BUS Service]\n"
        f"Name={BUS_NAME}\n"
        f'Exec="{sys.executable}" "{script}"\n',
        encoding="utf-8",
    )
    _reload_desktop()
    print("Installed. Press Alt+Space and type:  j what time is it")
    print(f"  {PLUGIN_FILE}\n  {SERVICE_FILE}")


def uninstall() -> None:
    for path in (PLUGIN_FILE, SERVICE_FILE):
        path.unlink(missing_ok=True)
    _reload_desktop()
    print("Removed the JARVIS KRunner plugin.")


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    elif "--uninstall" in sys.argv:
        uninstall()
    else:
        serve()
