"""Media control tool.

The volume / mute / transport primitives live in friday/media_control.py so the
launcher's fast path can call them without importing FastMCP. This module is
the MCP-facing wrapper. Finding and starting music goes through the Spotify
Web API (friday/spotify.py) once it's signed in; until then, through web
search as before.
"""
import asyncio
import ctypes
import logging
import re
import urllib.parse
from mcp.server.fastmcp import FastMCP

from friday import lyrics as lyrics_lookup
from friday import plat
from friday import spotify
from friday.media_control import (
    now_playing,
    open_uri,
    _get_app_volume,
    _get_master_volume,
    _set_app_volume,
    _set_master_volume,
    next_track as _next_track,
    play_pause_media as _play_pause_media,
    previous_track as _previous_track,
)


def _get_spotify_window_title() -> str | None:
    """Read the Spotify window title. Returns 'Artist - Track' when playing,
    or None if Spotify isn't running or nothing is playing."""
    if not plat.IS_WINDOWS:
        return now_playing("spotify") or now_playing()
    import ctypes.wintypes
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


logger = logging.getLogger("friday-agent")

_KINDS = ("track", "playlist", "album", "artist")
_SIGN_IN = "Spotify isn't signed in on this OS; run: uv run python -m friday.spotify login"


def _web_search_uri(query: str, kind: str) -> str | None:
    """spotify:<kind>:<id> from web search: the way until Spotify is signed in."""
    from ddgs import DDGS
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"{query} site:open.spotify.com/{kind}", max_results=5):
                match = re.search(rf"open\.spotify\.com/{kind}/([a-zA-Z0-9]+)", r.get("href", ""))
                if match:
                    return f"spotify:{kind}:{match.group(1)}"
    except Exception as e:
        logger.warning("Spotify web search failed: %s", e)
    return None


def _play_without_api(query: str, kind: str) -> str:
    import threading
    import time

    uri = _web_search_uri(query, kind)
    if not uri:
        plat.open_target(f"spotify:search:{urllib.parse.quote(query)}")
        return f"Couldn't find an exact match, but I opened the Spotify search for '{query}'."
    if not plat.IS_WINDOWS:
        # Spotify's MPRIS OpenUri plays the URI; opening it only navigates.
        if not open_uri("spotify", uri):
            plat.open_target("spotify:")
            for _ in range(16):         # a few seconds for Spotify to come up
                time.sleep(0.5)
                if open_uri("spotify", uri):
                    break
            else:
                return "Spotify didn't start."
        return f"Playing what web search found for {query}."
    plat.open_target(uri)
    if kind == "track":
        return "Pulled up the track — hit play when ready."
    # Spotify URIs navigate but don't auto-play for playlists/albums.
    # Wait for Spotify to load, then send media play key.
    def _delayed_play():
        time.sleep(2.5)
        _play_pause_media()
    threading.Thread(target=_delayed_play, daemon=True).start()
    return f"Found the {kind} and started playing it."


async def _song_from_lyrics(lyrics: str) -> tuple[str, str] | None:
    """("Title by Artist", what to tell him) for the song these lyrics are from."""
    found = await asyncio.to_thread(lyrics_lookup.identify, lyrics)
    if not found:
        return None
    title, artist = found
    return f"{title} by {artist}", f"Those lyrics are from {title} by {artist}. "


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
        return _play_pause_media()

    @mcp.tool(name="next_track")
    def next_track() -> str:
        """Skip to the next media track."""
        return _next_track()

    @mcp.tool(name="previous_track")
    def previous_track() -> str:
        """Go back to the previous media track."""
        return _previous_track()
        
    @mcp.tool(name="current_track")
    async def current_track() -> str:
        """Get the currently playing track from Spotify.
        Use when the user asks 'what's playing', 'what song is this',
        or 'what's the current track'."""
        if spotify.ready():
            try:
                playing = await asyncio.to_thread(spotify.now_playing)
                if not playing:
                    return "Nothing is playing on Spotify right now."
                item, is_playing = playing
                return f"Now playing: {item.label()}." if is_playing else f"Paused on {item.label()}."
            except spotify.SpotifyError as e:
                logger.warning("Spotify now-playing failed, reading the player instead: %s", e)
        title = _get_spotify_window_title()
        if title:
            # Title format is "Artist - Track Name"
            return f"Now playing: {title}."
        return "Nothing is playing on Spotify right now."

    @mcp.tool(name="play_music")
    async def play_music(query: str = "", type: str = "track", lyrics: str = "") -> str:
        """Play music on Spotify, started right away: a song, playlist, album or artist.

        query: what he asked for, e.g. "Circles by Post Malone", "Night Car",
            "study music". Keep the artist in it when he names one.
        type: "track" (default) for one song; "playlist" for a playlist, or for a
            mood or activity ("something to study to", "play some music");
            "album"; or "artist" for that artist's music in general ("play some Drake").
        lyrics: when he sings or quotes words from the song, or says "the song
            that goes like ...", put those words here exactly as heard and leave
            query empty; they are looked up to name the song. Never search lyrics
            through query or web search.
        """
        kind = type if type in _KINDS else "track"
        note = ""
        if lyrics.strip():
            found = await _song_from_lyrics(lyrics)
            if not found:
                return f'Couldn\'t tell which song goes like "{lyrics}".'
            query, note = found
            kind = "track"
        if not query.strip():
            return "Nothing to play: no song, playlist or lyrics were given."
        if spotify.ready():
            try:
                item = await asyncio.to_thread(spotify.find, query, kind)
                if not item:
                    return note + f"Couldn't find {query} on Spotify."
                await asyncio.to_thread(spotify.play, item)
                return note + f"Playing {item.label()}."
            except spotify.NotSignedIn as e:
                logger.warning("%s Falling back to web search.", e)
            except spotify.SpotifyError as e:
                return note + f"Spotify: {e}"
        return note + await asyncio.to_thread(_play_without_api, query, kind)

    @mcp.tool(name="queue_song")
    async def queue_song(query: str = "", lyrics: str = "") -> str:
        """Add a song to the Spotify queue to play after the current one
        ("queue up X", "play X next"). query and lyrics work as in play_music."""
        if not spotify.ready():
            return _SIGN_IN
        note = ""
        if lyrics.strip():
            found = await _song_from_lyrics(lyrics)
            if not found:
                return f'Couldn\'t tell which song goes like "{lyrics}".'
            query, note = found
        if not query.strip():
            return "Nothing to queue: no song or lyrics were given."
        try:
            item = await asyncio.to_thread(spotify.find, query, "track")
            if not item:
                return note + f"Couldn't find {query} on Spotify."
            await asyncio.to_thread(spotify.queue, item)
        except spotify.SpotifyError as e:
            return note + f"Spotify: {e}"
        return note + f"Queued {item.label()}."

    @mcp.tool(name="like_current_song")
    async def like_current_song() -> str:
        """Save the song playing on Spotify to his Liked Songs ("like this
        song", "save this one")."""
        if not spotify.ready():
            return _SIGN_IN
        try:
            playing = await asyncio.to_thread(spotify.now_playing)
            if not playing:
                return "Nothing is playing on Spotify right now."
            await asyncio.to_thread(spotify.like, playing[0])
        except spotify.SpotifyError as e:
            return f"Spotify: {e}"
        return f"Saved {playing[0].label()} to Liked Songs."
