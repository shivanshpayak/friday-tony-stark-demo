"""Volume, mute, and media-transport control.

Deliberately free of any MCP/FastMCP import: this module is loaded by
friday_launcher.py for the fast path, and the launcher process must not pull
in the tool server's dependency tree.

friday/tools/media.py wraps these as MCP tools; friday/fastpath/actions.py
binds them to voice commands. One implementation, two callers.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

_WIN = sys.platform == "win32"

if _WIN:
    import keyboard
    from comtypes import CoInitialize, CoUninitialize
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume


# --- Linux backends (PipeWire via wpctl/pactl, players via MPRIS) -----------

_SINK = "@DEFAULT_AUDIO_SINK@"
_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"


def _run(argv: list[str]) -> str:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or f"{argv[0]} failed")
    return res.stdout


def _mpris_players() -> list[str]:
    """Bus names of every MPRIS media player, currently-playing ones first."""
    out = _run(["busctl", "--user", "list", "--no-legend", "--no-pager"])
    names = [line.split()[0] for line in out.splitlines()
             if line.startswith(_MPRIS_PREFIX)]

    def _is_playing(name: str) -> bool:
        try:
            return '"Playing"' in _run(
                ["busctl", "--user", "get-property", name, _MPRIS_PATH,
                 _MPRIS_PLAYER, "PlaybackStatus"])
        except Exception:
            return False

    return sorted(names, key=lambda n: not _is_playing(n))


def _mpris_call(method: str) -> bool:
    """Send a transport command to the most relevant player. False if none."""
    players = _mpris_players()
    if not players:
        return False
    _run(["busctl", "--user", "call", players[0], _MPRIS_PATH, _MPRIS_PLAYER, method])
    return True


def _playing(name: str) -> bool:
    try:
        return '"Playing"' in _run(["busctl", "--user", "get-property", name, _MPRIS_PATH,
                                    _MPRIS_PLAYER, "PlaybackStatus"])
    except Exception:
        return False


def pause_everything() -> int:
    """Pause every player that's playing — a YouTube tab, Spotify, anything. How many."""
    if _WIN:
        return 0
    paused = 0
    for name in _mpris_players():
        if _playing(name):
            _run(["busctl", "--user", "call", name, _MPRIS_PATH, _MPRIS_PLAYER, "Pause"])
            paused += 1
    return paused


def play_player(hint: str) -> bool:
    """Press Play on the player whose bus name contains `hint` ("spotify").
    True once it reports Playing."""
    if _WIN:
        return False
    for name in _mpris_players():
        if hint in name.lower():
            _run(["busctl", "--user", "call", name, _MPRIS_PATH, _MPRIS_PLAYER, "Play"])
            return _playing(name)
    return False


def open_uri(hint: str, uri: str) -> bool:
    """Have the running player whose bus name contains `hint` play `uri`
    (Spotify takes spotify:track:… and starts it). False if it isn't running."""
    if _WIN:
        return False
    for name in _mpris_players():
        if hint in name.lower():
            _run(["busctl", "--user", "call", name, _MPRIS_PATH, _MPRIS_PLAYER,
                  "OpenUri", "s", uri])
            return True
    return False


def now_playing(player_hint: str = "") -> str | None:
    """'Artist - Title' from MPRIS metadata, or None when nothing is loaded.

    `player_hint` (e.g. "spotify") restricts the lookup to matching players.
    """
    if _WIN:
        return None
    try:
        players = _mpris_players()
    except Exception:
        return None
    for name in players:
        if player_hint and player_hint.lower() not in name.lower():
            continue
        try:
            meta = json.loads(_run(
                ["busctl", "--user", "--json=short", "get-property", name,
                 _MPRIS_PATH, _MPRIS_PLAYER, "Metadata"])).get("data", {})
        except Exception:
            continue
        title = (meta.get("xesam:title") or {}).get("data") or ""
        artists = (meta.get("xesam:artist") or {}).get("data") or []
        if isinstance(artists, str):
            artists = [artists]
        artist = ", ".join(a for a in artists if a)
        if title:
            return f"{artist} - {title}" if artist else title
    return None


def _sink_inputs() -> list[dict]:
    """Per-app playback streams, as reported by `pactl`."""
    return json.loads(_run(["pactl", "--format=json", "list", "sink-inputs"]))


def _find_sink_input(app_name: str) -> tuple[dict, str] | None:
    needle = app_name.lower()
    for stream in _sink_inputs():
        props = stream.get("properties", {})
        for key in ("application.name", "application.process.binary", "media.name"):
            value = props.get(key) or ""
            if needle in value.lower():
                label = props.get("application.name") or value
                return stream, label
    return None


def _stream_volume(stream: dict) -> float:
    levels = [
        int(re.sub(r"\D", "", ch.get("value_percent", "0")) or 0)
        for ch in (stream.get("volume") or {}).values()
    ]
    return (sum(levels) / len(levels) / 100.0) if levels else 0.0


# --- master endpoint -------------------------------------------------------

def _endpoint_volume():
    """Return the default speaker endpoint's volume interface.

    Caller is responsible for CoInitialize/CoUninitialize around this.
    """
    return AudioUtilities.GetSpeakers().EndpointVolume


def _set_master_volume(level: float) -> str:
    """Set system master volume. `level` is 0.0 to 1.0."""
    if not _WIN:
        _run(["wpctl", "set-volume", _SINK, f"{max(0.0, min(1.0, level)):.2f}"])
        return f"System volume set to {int(level * 100)}%."
    CoInitialize()
    try:
        _endpoint_volume().SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)
        return f"System volume set to {int(level * 100)}%."
    finally:
        CoUninitialize()


def _get_master_volume() -> float:
    """Return system master volume as a fraction 0.0-1.0."""
    if not _WIN:
        # "Volume: 0.40" or "Volume: 0.40 [MUTED]"
        match = re.search(r"[\d.]+", _run(["wpctl", "get-volume", _SINK]))
        return float(match.group()) if match else 0.0
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
    if not _WIN:
        _run(["wpctl", "set-mute", _SINK, "1" if muted else "0"])
        return "Muted." if muted else "Unmuted."
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
    if not _WIN:
        found = _find_sink_input(app_name)
        if found is None:
            return (f"Couldn't find an audio session for '{app_name}'. "
                    "It might not be playing anything right now.")
        stream, label = found
        percent = int(round(max(0.0, min(1.0, level)) * 100))
        _run(["pactl", "set-sink-input-volume", str(stream["index"]), f"{percent}%"])
        return f"{label} volume set to {percent}%."
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
    if not _WIN:
        found = _find_sink_input(app_name)
        if found is None:
            return None
        stream, label = found
        return _stream_volume(stream), label
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


# --- ducking (music turned down while an alarm rings) ----------------------

# JARVIS's own short sounds (chimes, the alarm). PipeWire remembers each app's
# volume, so one of these ducked and gone before the restore would leave every
# later chime at the ducked level.
_OWN_PLAYERS = ("pw-play", "paplay", "aplay")
# A stream whose volume moved more than this since we ducked it was changed by
# hand mid-alarm, and is left where the user put it.
_DUCK_TOLERANCE = 0.01


def _channel_values(stream: dict) -> list[int]:
    """Raw per-channel volumes (65536 = 100%), in channel-map order."""
    return [int(ch.get("value", 0)) for ch in (stream.get("volume") or {}).values()]


def _client_pids() -> dict[str, int]:
    """PipeWire client index → process id. Streams from ALSA apps (JARVIS's
    own voice among them) carry no pid of their own; their client does."""
    pids = {}
    for client in json.loads(_run(["pactl", "--format=json", "list", "clients"])):
        try:
            pids[str(client["index"])] = int(client["properties"]["application.process.id"])
        except (KeyError, TypeError, ValueError):
            continue
    return pids


def _stream_pid(stream: dict, client_pids: dict[str, int]) -> int | None:
    try:
        return int(stream.get("properties", {})["application.process.id"])
    except (KeyError, TypeError, ValueError):
        return client_pids.get(str(stream.get("client")))


def _session_key(session) -> str:
    """Windows: one process can own several audio sessions."""
    return f"{session.ProcessId}:{getattr(session, 'InstanceIdentifier', '') or ''}"


def duck_playback(factor: float, keep_pids: frozenset[int] = frozenset()) -> list[dict]:
    """Turn every playing stream down to `factor` of its volume.

    Returns what unduck_playback() needs to put them back. Streams belonging to
    `keep_pids` (JARVIS's own processes, so its voice stays clear) are left alone.
    """
    factor = max(0.0, min(1.0, factor))
    ducked: list[dict] = []
    if not _WIN:
        client_pids = _client_pids()
        for stream in _sink_inputs():
            props = stream.get("properties", {})
            app = f'{props.get("application.name", "")} {props.get("application.process.binary", "")}'
            if (stream.get("corked") or _stream_pid(stream, client_pids) in keep_pids
                    or any(player in app for player in _OWN_PLAYERS)):
                continue
            volume = _channel_values(stream)
            if not any(volume):
                continue
            lowered = [int(v * factor) for v in volume]
            _run(["pactl", "set-sink-input-volume", str(stream["index"]), *map(str, lowered)])
            ducked.append({"id": stream["index"], "volume": volume, "ducked": lowered})
        return ducked
    CoInitialize()
    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                # State 1 = AudioSessionStateActive: something is playing.
                if (not session.Process or session.ProcessId in keep_pids
                        or getattr(session, "State", 1) != 1):
                    continue
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                level = float(vol.GetMasterVolume())
                if level <= 0:
                    continue
                vol.SetMasterVolume(level * factor, None)
                ducked.append({"id": _session_key(session), "volume": [level],
                               "ducked": [level * factor]})
            except Exception:
                continue
        return ducked
    finally:
        CoUninitialize()


def _moved(now: list[float], ducked: list[float], full_scale: float) -> bool:
    return len(now) != len(ducked) or any(
        abs(a - b) > _DUCK_TOLERANCE * full_scale for a, b in zip(now, ducked))


def unduck_playback(ducked: list[dict]) -> int:
    """Put back what duck_playback() turned down; return how many.

    Skips a stream that has gone, or whose volume was changed by hand since.
    """
    restored = 0
    if not ducked:
        return restored
    if not _WIN:
        current = {s["index"]: _channel_values(s) for s in _sink_inputs()}
        for item in ducked:
            now = current.get(item["id"])
            if now is None or _moved(now, item["ducked"], 65536):
                continue
            _run(["pactl", "set-sink-input-volume", str(item["id"]),
                  *map(str, item["volume"])])
            restored += 1
        return restored
    CoInitialize()
    try:
        by_key = {item["id"]: item for item in ducked}
        for session in AudioUtilities.GetAllSessions():
            try:
                item = by_key.pop(_session_key(session), None)
                if item is None:
                    continue
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                if _moved([float(vol.GetMasterVolume())], item["ducked"], 1.0):
                    continue
                vol.SetMasterVolume(item["volume"][0], None)
                restored += 1
            except Exception:
                continue
        return restored
    finally:
        CoUninitialize()


# --- transport -------------------------------------------------------------

_NO_PLAYER = "Nothing is playing right now."

def play_pause_media() -> str:
    if not _WIN:
        return "Toggled playback." if _mpris_call("PlayPause") else _NO_PLAYER
    keyboard.send("play/pause media")
    return "Toggled playback."


def next_track() -> str:
    if not _WIN:
        return "Skipped to next track." if _mpris_call("Next") else _NO_PLAYER
    keyboard.send("next track")
    return "Skipped to next track."


def previous_track() -> str:
    if not _WIN:
        return "Went to previous track." if _mpris_call("Previous") else _NO_PLAYER
    keyboard.send("previous track")
    return "Went to previous track."


def stop_media() -> str:
    if not _WIN:
        return "Stopped playback." if _mpris_call("Stop") else _NO_PLAYER
    keyboard.send("stop media")
    return "Stopped playback."
