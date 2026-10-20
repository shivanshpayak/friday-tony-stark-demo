"""Music through the Spotify Web API (friday/spotify.py, friday/lyrics.py,
play_music & co. in friday/tools/media.py). The requests are the ones that
went wrong in the logs."""
import asyncio
import json
import time

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from friday import lyrics, spotify
from friday.spotify import Item
from friday.tools import media


def _track(name, *artists):
    return Item("track", f"spotify:track:{abs(hash((name, artists))) % 10**8}", name, artists)


# --- picking the right result ----------------------------------------------

def test_the_artist_is_split_off_the_title():
    assert spotify.split_artist("Circles by Post Malone") == ("Circles", "Post Malone")
    assert spotify.split_artist("Havana") == ("Havana", None)


def test_the_named_artists_song_beats_others_with_the_same_title():
    other, right = _track("Circles", "Mac Miller"), _track("Circles", "Post Malone")
    assert spotify.best([other, right], "Circles by Post Malone") == right


def test_covers_and_karaoke_lose_to_the_original():
    """Logged: "The Color Violet" played a violin version."""
    cover = _track("The Color Violet (Violin Cover)", "Vitamin String Quartet")
    karaoke = _track("The Color Violet - Karaoke Version", "Sing King")
    original = _track("The Color Violet", "Tory Lanez")
    assert spotify.best([cover, karaoke, original], "The Color Violet") == original


def test_asking_for_a_version_gets_that_version():
    original, acoustic = _track("Circles", "Post Malone"), _track("Circles - Acoustic", "Post Malone")
    assert spotify.best([original, acoustic], "Circles acoustic") == acoustic


def test_a_remaster_is_not_treated_as_a_different_version():
    remaster = _track("Bohemian Rhapsody - Remastered 2011", "Queen")
    live = _track("Bohemian Rhapsody - Live Aid", "Queen")
    assert spotify.best([live, remaster], "Bohemian Rhapsody") == remaster


def test_equal_matches_keep_spotifys_order():
    first, second = _track("Havana (feat. Young Thug)", "Camila Cabello"), _track("Havana", "Kenny G")
    assert spotify.best([first, second], "Havana") == first


def test_close_wording_keeps_spotifys_order():
    """Logged 10-19: an unknown exact "It's A Hard Knock Life" beat the
    famous "It's the Hard-Knock Life" that Spotify listed first."""
    famous = _track("It's the Hard-Knock Life", "Aileen Quinn")
    exact = _track("It's A Hard Knock Life", "D.T.E")
    assert spotify.best([famous, exact], "It's a Hard Knock Life") == famous


def test_an_artist_named_like_the_title_gets_no_extra_credit():
    """Logged 10-19: "Popcorn" picked "Popcorn" by Popcorn."""
    first, same_name = _track("Popcorn", "Crazy Frog"), _track("Popcorn", "Popcorn")
    assert spotify.best([first, same_name], "Popcorn") == first


def test_playlist_names_said_in_full_or_spelled_out():
    """His playlist "idek bro ig", said as words and as letters."""
    assert spotify._abbreviates("idek bro ig", "i dont even know bro i guess")
    assert spotify._abbreviates("idek bro ig", "i d e k b r o i g")
    assert not spotify._abbreviates("idek bro ig", "i dont even know")
    assert not spotify._abbreviates("night car", "upbeat")


def test_a_title_with_by_in_it_still_matches():
    song = _track("Stand by Me", "Ben E. King")
    assert spotify.best([_track("Stand", "R.E.M."), song], "Stand by Me") == song


def test_a_misheard_artist_still_helps():
    """Speech recognition wrote "Tori Lenais" for Tory Lanez."""
    other, right = _track("The Color Violet", "Someone Else"), _track("The Color Violet", "Tory Lanez")
    assert spotify.best([other, right], "The Color Violet by Tori Lenais") == right


# --- the API, against a fake Spotify -----------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    """A signed-in client talking to `routes` instead of Spotify."""
    token = tmp_path / "spotify_token.json"
    token.write_text(json.dumps({"access_token": "A", "refresh_token": "R",
                                 "expires_at": time.time() + 3600}))
    monkeypatch.setattr(spotify, "TOKEN_FILE", token)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client")
    seen, routes = [], {}

    def handle(request: httpx.Request):
        seen.append(request)
        key = (request.method, request.url.path)
        reply = routes.get(key)
        if callable(reply):
            reply = reply(request)
        if reply is None:
            return httpx.Response(404, json={"error": {"status": 404, "message": f"no route {key}"}})
        status, body = reply
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    monkeypatch.setattr(spotify, "_http", httpx.Client(transport=httpx.MockTransport(handle)))
    return type("Api", (), {"routes": routes, "seen": seen, "token": token})


def _raw_track(name, artist, uri):
    return {"name": name, "uri": uri, "artists": [{"name": artist}]}


def test_his_own_playlist_is_found_even_when_misheard(api):
    """Logged: "Night Card" for his playlist Night Car."""
    api.routes[("GET", "/v1/me/playlists")] = (200, {"items": [
        {"name": "Gym", "uri": "spotify:playlist:gym", "owner": {"display_name": "me"}},
        {"name": "Night Car", "uri": "spotify:playlist:nc", "owner": {"display_name": "me"}}]})
    assert spotify.find("night card", "playlist").uri == "spotify:playlist:nc"
    assert not any(r.url.path == "/v1/search" for r in api.seen)


def test_search_uses_both_the_plain_query_and_artist_filters(api):
    api.routes[("GET", "/v1/search")] = lambda r: (200, {"tracks": {"items": [
        _raw_track("Circles", "Post Malone", "spotify:track:pm")
        if "artist:" in r.url.params["q"] else _raw_track("Circles", "Mac Miller", "spotify:track:mm")]}})
    assert spotify.find("Play Circles by Post Malone", "track").uri == "spotify:track:pm"
    queries = sorted(r.url.params["q"] for r in api.seen)
    assert queries == ["Circles by Post Malone", 'track:"Circles" artist:"Post Malone"']
    assert all(r.url.params["limit"] == "10" for r in api.seen)   # the Feb 2026 maximum


def test_a_song_plays_by_itself_on_this_computer(api):
    """Not inside a playlist, which he asked for in the logs."""
    api.routes[("GET", "/v1/me/player/devices")] = (200, {"devices": [
        {"id": "phone", "name": "iPhone", "type": "Smartphone", "is_active": True},
        {"id": "laptop", "name": "shivp", "type": "Computer", "is_active": False}]})
    api.routes[("PUT", "/v1/me/player/play")] = (204, None)
    spotify.play(Item("track", "spotify:track:x", "X", ("Y",)))
    request = api.seen[-1]
    assert request.url.params["device_id"] == "laptop"
    assert json.loads(request.content) == {"uris": ["spotify:track:x"]}


def test_a_playlist_plays_as_a_context(api):
    api.routes[("GET", "/v1/me/player/devices")] = (200, {"devices": [
        {"id": "laptop", "name": "shivp", "type": "Computer"}]})
    api.routes[("PUT", "/v1/me/player/play")] = (204, None)
    spotify.play(Item("playlist", "spotify:playlist:nc", "Night Car"))
    assert json.loads(api.seen[-1].content) == {"context_uri": "spotify:playlist:nc"}


def test_spotify_is_started_when_it_is_not_running(api, monkeypatch):
    polls = {"n": 0}

    def devices(_):
        polls["n"] += 1
        found = [{"id": "laptop", "name": "shivp", "type": "Computer"}] if polls["n"] >= 3 else []
        return 200, {"devices": found}

    api.routes[("GET", "/v1/me/player/devices")] = devices
    opened = []
    monkeypatch.setattr(spotify, "_open_desktop_app", lambda: opened.append(True))
    monkeypatch.setattr(spotify.time, "sleep", lambda s: None)
    assert spotify.device_id() == "laptop" and opened == [True]


def test_an_expired_token_is_refreshed_and_the_old_refresh_token_kept(api):
    api.token.write_text(json.dumps({"access_token": "old", "refresh_token": "R",
                                     "expires_at": time.time() - 10}))
    api.routes[("POST", "/api/token")] = (200, {"access_token": "new", "expires_in": 3600})
    api.routes[("GET", "/v1/me/player/currently-playing")] = (204, None)
    assert spotify.now_playing() is None
    saved = json.loads(api.token.read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "R"
    assert api.seen[-1].headers["Authorization"] == "Bearer new"


def test_a_rejected_token_is_refreshed_once_and_retried(api):
    calls = {"n": 0}

    def playing(_):
        calls["n"] += 1
        return (401, {"error": {"message": "expired"}}) if calls["n"] == 1 else (204, None)

    api.routes[("GET", "/v1/me/player/currently-playing")] = playing
    api.routes[("POST", "/api/token")] = (200, {"access_token": "new", "expires_in": 3600})
    assert spotify.now_playing() is None and calls["n"] == 2


def test_rate_limits_and_premium_errors_say_so(api):
    api.routes[("GET", "/v1/search")] = (429, {"error": {"message": "slow down"}})
    with pytest.raises(spotify.SpotifyError, match="rate limiting"):
        spotify.find("Havana")
    api.routes[("POST", "/v1/me/player/queue")] = (403, {"error": {"message": "Premium required"}})
    with pytest.raises(spotify.SpotifyError, match="Premium"):
        spotify.queue(Item("track", "spotify:track:x", "X"))


def test_liking_uses_the_library_endpoint(api):
    """February 2026: PUT /me/tracks became PUT /me/library?uris=..."""
    api.routes[("PUT", "/v1/me/library")] = (200, None)
    spotify.like(Item("track", "spotify:track:x", "X"))
    assert api.seen[-1].url.params["uris"] == "spotify:track:x"


# --- lyrics -------------------------------------------------------------------

def test_the_lyrics_answer_is_parsed():
    assert lyrics.parse_answer("Can't Stop the Feeling! — Justin Timberlake") == (
        "Can't Stop the Feeling!", "Justin Timberlake")
    assert lyrics.parse_answer("**I Don't Care — Ed Sheeran & Justin Bieber**") == (
        "I Don't Care", "Ed Sheeran & Justin Bieber")
    assert lyrics.parse_answer("Fall Into Me - Forest Blakk") == ("Fall Into Me", "Forest Blakk")
    assert lyrics.parse_answer("unknown") is None
    assert lyrics.parse_answer("") is None


def test_without_a_searched_answer_it_asks_without_search(monkeypatch):
    asked = []

    def ask(text, *, search):
        asked.append(search)
        return "" if search else "Can't Stop the Feeling! — Justin Timberlake"

    monkeypatch.setattr(lyrics, "_ask", ask)
    assert lyrics.identify("it's so electric") == ("Can't Stop the Feeling!", "Justin Timberlake")
    assert asked == [True, False]


# --- the tools ------------------------------------------------------------------

@pytest.fixture
def tools():
    mcp = FastMCP(name="test")
    media.register(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


def test_lyrics_are_named_then_played(tools, monkeypatch):
    """Logged 10-18: nine tries at "it's so electric baby when I turn it on"."""
    monkeypatch.setattr(spotify, "ready", lambda: True)
    monkeypatch.setattr(lyrics, "identify", lambda words: ("Can't Stop the Feeling!", "Justin Timberlake"))
    found = []
    monkeypatch.setattr(spotify, "find", lambda q, kind: found.append((q, kind)) or
                        Item("track", "spotify:track:c", "CAN'T STOP THE FEELING!", ("Justin Timberlake",)))
    played = []
    monkeypatch.setattr(spotify, "play", played.append)
    reply = asyncio.run(tools["play_music"](lyrics="it's so electric baby when I turn it on"))
    assert found == [("Can't Stop the Feeling! by Justin Timberlake", "track")]
    assert played and reply.startswith("Those lyrics are from Can't Stop the Feeling!")
    assert "Playing CAN'T STOP THE FEELING! by Justin Timberlake." in reply


def test_not_signed_in_falls_back_to_web_search(tools, monkeypatch):
    monkeypatch.setattr(spotify, "ready", lambda: False)
    monkeypatch.setattr(media, "_play_without_api", lambda q, kind: f"web:{q}:{kind}")
    assert asyncio.run(tools["play_music"](query="Havana")) == "web:Havana:track"


def test_like_and_queue_need_sign_in(tools, monkeypatch):
    monkeypatch.setattr(spotify, "ready", lambda: False)
    assert "login" in asyncio.run(tools["like_current_song"]())
    assert "login" in asyncio.run(tools["queue_song"](query="Havana"))
