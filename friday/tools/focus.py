"""Focus mode — "focus for 45 minutes" (friday/focus.py holds the session)."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

from friday import focus, media_control, plat
from friday.config import FOCUS_CLOSE_APPS, FOCUS_PLAYLIST
from friday.scheduling import ScheduledItem, add, all_items, remove

if sys.platform == "win32":
    from friday.tools.apps import close_app
else:
    from friday.tools.apps_linux import close_app


def _clock(epoch: float) -> str:
    return time.strftime("%I:%M %p", time.localtime(epoch)).lstrip("0")


def _drop_focus_timer() -> None:
    for item in all_items():
        if item.label == focus.TIMER_LABEL:
            remove(item.id)


def _play_lofi() -> None:
    """Open the playlist, then press Play once Spotify is up (it may be starting)."""
    plat.open_target(FOCUS_PLAYLIST)
    for _ in range(12):
        time.sleep(1.5)
        if media_control.play_player("spotify"):
            return


def start(minutes: int = 45) -> str:
    minutes = max(1, min(int(minutes), 240))
    current = focus.active()
    if current:
        return f"Already focusing, sir, until {_clock(current['until'])}."
    closed = []
    for name in FOCUS_CLOSE_APPS:
        result = close_app(name)
        if result.startswith("Closed "):
            closed.append(result[len("Closed "):].rstrip("."))
    media_control.pause_everything()
    threading.Thread(target=_play_lofi, daemon=True).start()
    record = focus.begin(minutes, closed, focus.notification_count())
    # The end is a timer: it orbits the wallpaper as a moon, and when it fires
    # the agent speaks the recap.
    _drop_focus_timer()
    add(ScheduledItem(kind="timer", label=focus.TIMER_LABEL,
                      fire_at=datetime.fromtimestamp(record["until"]).isoformat(),
                      created_at=datetime.fromtimestamp(record["started"]).isoformat(),
                      message="Focus session's over, sir."))
    done = ["notifications held"] + ([f"{' and '.join(closed)} closed"] if closed else []) + ["lo-fi on"]
    return f"Focus mode until {_clock(record['until'])}: {', '.join(done)}."


def end() -> str:
    _drop_focus_timer()
    record = focus.finish()
    if not record:
        return "There's no focus session running, sir."
    return focus.recap(record, focus.notification_count())


def register(mcp):

    @mcp.tool(name="start_focus")
    def start_focus(minutes: int = 45) -> str:
        """Start a focus session for `minutes` (default 45): holds notifications
        (Do Not Disturb), closes distractions like Discord, pauses whatever is
        playing, puts lo-fi on in Spotify, dims the wallpaper, and recaps the
        session when time's up. Use for "focus for 45 minutes", "focus mode",
        "help me focus", "time to study"."""
        return start(minutes)

    @mcp.tool(name="end_focus")
    def end_focus() -> str:
        """End the focus session early: Do Not Disturb lifts, and you get a recap
        to tell him. Use for "stop focus mode", "I'm done focusing"."""
        return end()
