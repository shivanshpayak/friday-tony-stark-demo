"""Zero-argument actions bound to fast-path voice commands.

Each is a plain synchronous callable that performs one state change and
returns None. They are called from a thread-pool executor in the launcher, so
they must not assume an event loop.

Control functions are referenced through the `mc` module object (not imported
by name) so tests can monkeypatch the control layer.
"""
from __future__ import annotations

from friday import media_control as mc
from friday.config import FASTPATH_VOLUME_STEP

VOLUME_STEP = FASTPATH_VOLUME_STEP


def mute() -> None:
    mc.set_master_mute(True)


def unmute() -> None:
    mc.set_master_mute(False)


def play_pause() -> None:
    mc.play_pause_media()


def next_track() -> None:
    mc.next_track()


def previous_track() -> None:
    mc.previous_track()


def stop_media() -> None:
    mc.stop_media()


def louder() -> None:
    mc.adjust_master_volume(VOLUME_STEP)


def quieter() -> None:
    mc.adjust_master_volume(-VOLUME_STEP)
