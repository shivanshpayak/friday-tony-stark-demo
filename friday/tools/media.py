"""Media control tool."""
import ctypes
import ctypes.wintypes
import os
import keyboard
import urllib.parse
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, ISimpleAudioVolume
from mcp.server.fastmcp import FastMCP


def _set_master_volume(level: float) -> str:
    """Set system master volume. level is 0.0 to 1.0."""
    CoInitialize()
    try:
        volume = AudioUtilities.GetSpeakers().EndpointVolume
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)
        return f"System volume set to {int(level * 100)}%."
    finally:
        CoUninitialize()


def _get_master_volume() -> float:
    """Return system master volume as a fraction 0.0–1.0."""
    CoInitialize()
    try:
        volume = AudioUtilities.GetSpeakers().EndpointVolume
        return float(volume.GetMasterVolumeLevelScalar())
    finally:
        CoUninitialize()


def _set_app_volume(app_name: str, level: float) -> str:
    """Set volume for a specific app. app_name is matched case-insensitively."""
    CoInitialize()
    try:
        sessions = AudioUtilities.GetAllSessions()
        needle = app_name.lower()
        for session in sessions:
            if session.Process and needle in session.Process.name().lower():
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                vol.SetMasterVolume(max(0.0, min(1.0, level)), None)
                return f"{session.Process.name()} volume set to {int(level * 100)}%."
        return f"Couldn't find an audio session for '{app_name}'. It might not be playing anything right now."
    finally:
        CoUninitialize()


def _get_app_volume(app_name: str) -> tuple[float, str] | None:
    """Return (level 0.0–1.0, process_name) for the first matching app session,
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

def _get_spotify_window_title() -> str | None:
    """Read the Spotify window title. Returns 'Artist - Track' when playing,
    or None if Spotify isn't running or nothing is playing."""
    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible

    titles = []

    def callback(hwnd, _):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                # Spotify window class is Chrome_WidgetWin_1;
                # match by title containing known Spotify patterns.
                if title and ("Spotify" in title or "spotify" in title):
                    titles.append(title)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    EnumWindows(WNDENUMPROC(callback), 0)

    # Filter out generic titles (nothing playing).
    idle_titles = {"Spotify", "Spotify Free", "Spotify Premium"}
    for t in titles:
        if t not in idle_titles and " - " in t:
            return t  # "Artist - Track"
    return None


def register(mcp: FastMCP):

    @mcp.tool(name="set_volume")
    def set_volume(level: int, app: str = "") -> str:
        """Set volume level (0-100). If app is empty, sets system master volume.
        If app is specified, sets that app's volume. Use app names like
        'spotify', 'chrome' (for YouTube), 'discord', etc."""
        fraction = max(0, min(100, level)) / 100.0
        if app:
            return _set_app_volume(app, fraction)
        return _set_master_volume(fraction)

    @mcp.tool(name="adjust_volume")
    def adjust_volume(delta: int, app: str = "") -> str:
        """Change volume relative to its current level. `delta` is in
        percentage points: positive raises, negative lowers (e.g. delta=5
        means "+5%", delta=-10 means "-10%"). If `app` is empty, adjusts
        system master volume; otherwise adjusts that app's volume.
        Use when the user says "increase the volume by X", "turn it up a
        bit", "lower spotify by 20%", etc. Result is clamped to 0-100."""
        if app:
            current = _get_app_volume(app)
            if current is None:
                return f"Couldn't find an audio session for '{app}'. It might not be playing anything right now."
            cur_frac, proc_name = current
            new_frac = max(0.0, min(1.0, cur_frac + delta / 100.0))
            return _set_app_volume(app, new_frac)
        cur_frac = _get_master_volume()
        new_frac = max(0.0, min(1.0, cur_frac + delta / 100.0))
        return _set_master_volume(new_frac)

    @mcp.tool(name="play_pause_media")
    def play_pause_media() -> str:
        """Toggle play/pause for currently active media (like Spotify, YouTube)."""
        keyboard.send("play/pause media")
        return "Toggled playback."
        
    @mcp.tool(name="next_track")
    def next_track() -> str:
        """Skip to the next media track."""
        keyboard.send("next track")
        return "Skipped to next track."

    @mcp.tool(name="previous_track")
    def previous_track() -> str:
        """Go back to the previous media track."""
        keyboard.send("previous track")
        return "Went to previous track."
        
    @mcp.tool(name="current_track")
    def current_track() -> str:
        """Get the currently playing track from Spotify.
        Use when the user asks 'what's playing', 'what song is this',
        or 'what's the current track'."""
        title = _get_spotify_window_title()
        if title:
            # Title format is "Artist - Track Name"
            return f"Now playing: {title}."
        return "Nothing is playing on Spotify right now."

    @mcp.tool(name="search_spotify")
    async def search_spotify(query: str, type: str = "track") -> str:
        """Search Spotify and instantly auto-play a track, playlist, or album.
        Use this when the user says 'play <something> on spotify'.
        Set type to 'playlist' when the user asks for a playlist, 'album' for albums,
        or 'track' (default) for individual songs.
        """
        import asyncio
        from ddgs import DDGS
        import re

        def _get_spotify_uri():
            try:
                with DDGS() as ddgs:
                    search_query = f"{query} site:open.spotify.com/{type}"
                    results = list(ddgs.text(search_query, max_results=5))

                    for r in results:
                        link = r.get("href", "")
                        match = re.search(
                            rf"open\.spotify\.com/{type}/([a-zA-Z0-9]+)", link
                        )
                        if match:
                            return match.group(1)
                return None
            except Exception as e:
                print(f"DDGS Error: {e}")
                return None

        spotify_id = await asyncio.get_event_loop().run_in_executor(None, _get_spotify_uri)

        if spotify_id:
            try:
                import subprocess
                import time
                import threading
                # Use 'start' via cmd to open spotify: URIs
                uri = f"spotify:{type}:{spotify_id}"
                subprocess.Popen(
                    f'cmd /c start "" "{uri}"',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Spotify URIs navigate but don't auto-play for playlists/albums.
                # Wait for Spotify to load, then send media play key.
                if type in ("playlist", "album"):
                    def _delayed_play():
                        time.sleep(2.5)
                        keyboard.send("play/pause media")
                    threading.Thread(target=_delayed_play, daemon=True).start()
                label = {"track": "track", "playlist": "playlist", "album": "album"}.get(type, type)
                if type == "track":
                    return f"Pulled up the {label}, sir — hit play when ready."
                return f"Found the {label} and started playing it."
            except Exception as e:
                return f"Failed to auto-play Spotify {type}: {e}"
        else:
            escaped_query = urllib.parse.quote(query)
            try:
                import subprocess
                subprocess.Popen(
                    f'cmd /c start "" "spotify:search:{escaped_query}"',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return f"Couldn't find an exact match, but I opened the Spotify search for '{query}'."
            except Exception as e:
                return f"Failed to open Spotify: {e}"
