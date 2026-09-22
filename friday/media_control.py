"""Volume, mute, and media-transport control.

Deliberately free of any MCP/FastMCP import: this module is loaded by
friday_launcher.py for the fast path, and the launcher process must not pull
in the tool server's dependency tree.

friday/tools/media.py wraps these as MCP tools; friday/fastpath/actions.py
binds them to voice commands. One implementation, two callers.
"""
from __future__ import annotations

import keyboard
from comtypes import CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume


# --- master endpoint -------------------------------------------------------

def _endpoint_volume():
    """Return the default speaker endpoint's volume interface.

    Caller is responsible for CoInitialize/CoUninitialize around this.
    """
    return AudioUtilities.GetSpeakers().EndpointVolume


def _set_master_volume(level: float) -> str:
    """Set system master volume. `level` is 0.0 to 1.0."""
    CoInitialize()
    try:
        _endpoint_volume().SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)
        return f"System volume set to {int(level * 100)}%."
    finally:
        CoUninitialize()


def _get_master_volume() -> float:
    """Return system master volume as a fraction 0.0-1.0."""
    CoInitialize()
    try:
        return float(_endpoint_volume().GetMasterVolumeLevelScalar())
    finally:
        CoUninitialize()


def set_master_mute(muted: bool) -> str:
    """Mute or unmute the default output device.

    Explicit set rather than the toggle media key, so "mute" and "sound on"
    are idempotent and mean what they say.
    """
    CoInitialize()
    try:
        _endpoint_volume().SetMute(1 if muted else 0, None)
        return "Muted." if muted else "Unmuted."
    finally:
        CoUninitialize()


def adjust_master_volume(delta_points: int) -> str:
    """Change master volume by `delta_points` percentage points, clamped 0-100."""
    current = _get_master_volume()
    return _set_master_volume(max(0.0, min(1.0, current + delta_points / 100.0)))


# --- per-app sessions ------------------------------------------------------

def _set_app_volume(app_name: str, level: float) -> str:
    """Set volume for a specific app. `app_name` is matched case-insensitively."""
    CoInitialize()
    try:
        sessions = AudioUtilities.GetAllSessions()
        needle = app_name.lower()
        for session in sessions:
            if session.Process and needle in session.Process.name().lower():
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                vol.SetMasterVolume(max(0.0, min(1.0, level)), None)
                return f"{session.Process.name()} volume set to {int(level * 100)}%."
        return (f"Couldn't find an audio session for '{app_name}'. "
                "It might not be playing anything right now.")
    finally:
        CoUninitialize()


def _get_app_volume(app_name: str) -> tuple[float, str] | None:
    """Return (level 0.0-1.0, process_name) for the first matching app session,
    or None if no session was found."""
    CoInitialize()
    try:
        sessions = AudioUtilities.GetAllSessions()
        needle = app_name.lower()
        for session in sessions:
            if session.Process and needle in session.Process.name().lower():
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                return float(vol.GetMasterVolume()), session.Process.name()
        return None
    finally:
        CoUninitialize()


# --- transport -------------------------------------------------------------

def play_pause_media() -> str:
    keyboard.send("play/pause media")
    return "Toggled playback."


def next_track() -> str:
    keyboard.send("next track")
    return "Skipped to next track."


def previous_track() -> str:
    keyboard.send("previous track")
    return "Went to previous track."


def stop_media() -> str:
    keyboard.send("stop media")
    return "Stopped playback."
