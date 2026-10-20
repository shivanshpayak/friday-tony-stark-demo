"""Spotify through its Web API: find a song and start it, queue it, like it,
say what's on.

This replaced scraping web search for an open.spotify.com link and opening
the first one: it picked the wrong song or version, got rate limited after
~10 searches in a few minutes, and a single song only opened its page ("hit
play when ready").

Setup, once per OS install (the two OSes can't share runtime/):
  1. An app at developer.spotify.com/dashboard with the redirect URI
     http://127.0.0.1:47814/callback and the Web API ticked.
  2. SPOTIFY_CLIENT_ID=<its client ID> in .env. Sign-in uses PKCE, so there
     is no client secret.
  3. uv run python -m friday.spotify login
Since February 2026 the app's owner needs Premium; playback control always
did. The token is kept in runtime/spotify_token.json and refreshes itself.

API limits worth knowing (February 2026 changes for Development Mode apps):
search returns at most 10 results and no popularity, so results are ranked
here by how well they match what he said, with Spotify's order breaking ties.

Deliberately free of any MCP/LiveKit import.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger("friday-agent")

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "runtime" / "spotify_token.json"
PORT = 47814
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-modify",
    "playlist-read-private",
)
_ACCOUNTS = "https://accounts.spotify.com"
_API = "https://api.spotify.com/v1"
# A desktop app takes a few seconds to show up as a device after it starts.
_DEVICE_WAIT = 12.0

_http = httpx.Client(timeout=8.0)
_token_lock = threading.Lock()


class SpotifyError(RuntimeError):
    pass


class NotSignedIn(SpotifyError):
    pass


def client_id() -> str:
    return os.getenv("SPOTIFY_CLIENT_ID", "").strip()


def ready() -> bool:
    """Set up and signed in, so the API can be used."""
    return bool(client_id()) and TOKEN_FILE.exists()


# --- sign-in (PKCE) --------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _save_token(token: dict) -> dict:
    token = dict(token)
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def login(open_browser: bool = True, timeout: float = 300.0) -> None:
    """Sign in through the browser and keep the token. Run once per OS."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    cid = client_id()
    if not cid:
        raise SpotifyError("SPOTIFY_CLIENT_ID is not set in .env.")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    url = f"{_ACCOUNTS}/authorize?" + urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    reply: dict[str, str] = {}

    class _Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            reply.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
            ok = "code" in reply and reply.get("state") == state
            page = ("FRIDAY is signed in to Spotify. You can close this tab." if ok
                    else f"Sign-in failed: {reply.get('error', 'state mismatch')}")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", PORT), _Callback)
    server.timeout = 1.0
    print("Opening Spotify sign-in. If no browser opens, visit:\n" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout
    try:
        while not reply and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not reply:
        raise SpotifyError("Timed out waiting for the Spotify sign-in.")
    if reply.get("state") != state or "code" not in reply:
        raise SpotifyError(f"Spotify sign-in failed: {reply.get('error', 'state mismatch')}")
    response = _http.post(f"{_ACCOUNTS}/api/token", data={
        "grant_type": "authorization_code",
        "code": reply["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": cid,
        "code_verifier": verifier,
    })
    if response.status_code != 200:
        raise SpotifyError(f"Spotify token exchange failed: {response.text[:200]}")
    _save_token(response.json())


def _access_token(force_refresh: bool = False) -> str:
    with _token_lock:
        try:
            token = json.loads(TOKEN_FILE.read_text())
        except (OSError, ValueError):
            raise NotSignedIn("Spotify isn't signed in.") from None
        if force_refresh or token.get("expires_at", 0) - 60 < time.time():
            response = _http.post(f"{_ACCOUNTS}/api/token", data={
                "grant_type": "refresh_token",
                "refresh_token": token.get("refresh_token", ""),
                "client_id": client_id(),
            })
            if response.status_code == 400:
                raise NotSignedIn("The Spotify sign-in expired.")
            if response.status_code != 200:
                raise SpotifyError(f"Couldn't refresh the Spotify sign-in ({response.status_code}).")
            fresh = response.json()
            # PKCE refresh tokens rotate; keep the old one if none came back.
            fresh.setdefault("refresh_token", token.get("refresh_token"))
            token = _save_token(fresh)
        return token["access_token"]


# --- HTTP ------------------------------------------------------------------

def _api(method: str, path: str, *, params: dict | None = None,
         body: dict | None = None) -> dict | None:
    response = None
    for attempt in range(2):
        try:
            response = _http.request(
                method, _API + path, params=params, json=body,
                headers={"Authorization": f"Bearer {_access_token(force_refresh=attempt > 0)}"})
        except httpx.HTTPError as e:
            raise SpotifyError(f"Couldn't reach Spotify ({e.__class__.__name__}).") from e
        if response.status_code != 401:
            break
    if response.status_code < 300:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") if isinstance(error, dict) else str(error)
    if response.status_code == 429:
        raise SpotifyError("Spotify is rate limiting requests right now.")
    if response.status_code == 403 and "premium" in (message or "").lower():
        raise SpotifyError("Spotify says this needs Premium.")
    raise SpotifyError(message or f"Spotify returned {response.status_code}.")


# --- finding things --------------------------------------------------------

@dataclass(frozen=True)
class Item:
    kind: str                       # track, playlist, album, artist
    uri: str
    name: str
    artists: tuple[str, ...] = ()

    def label(self) -> str:
        return f"{self.name} by {', '.join(self.artists)}" if self.artists else self.name


# Versions nobody means unless they say so.
_VERSIONS = ("karaoke", "instrumental", "cover", "tribute", "originally performed",
             "made famous", "in the style of", "sped up", "slowed", "reverb",
             "nightcore", "8d", "remix", "acoustic", "live", "lofi", "lo fi",
             "piano version")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("’", "'").replace("$", "s")
    return " ".join(re.sub(r"[^\w\s']", " ", text).replace("'", "").split())


def _base(name: str) -> str:
    """A title without its " - Remastered 2011" / "(feat. X)" tail."""
    name = re.split(r"\s+-\s+", name, maxsplit=1)[0]
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", name).strip() or name


def split_artist(query: str) -> tuple[str, str | None]:
    """("Circles", "Post Malone") from "Circles by Post Malone"."""
    match = re.match(r"^(.+?)\s+by\s+(.+)$", query.strip(), flags=re.I)
    if not match:
        return query.strip(), None
    return match.group(1).strip(), match.group(2).strip()


def _has_word(phrase: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _sound(text: str) -> str:
    """Roughly how a name sounds: each word's first letter and its consonants.
    Speech recognition spells names by ear ("Tori Lenais" for Tory Lanez),
    and both come out "tr lns"; spelling similarity alone rated that pair
    below Justin Timberlake vs Justin Bieber."""
    keys = []
    for word in _norm(text).split():
        word = word.replace("ph", "f").replace("ck", "k").translate(str.maketrans("zcq", "skk"))
        keys.append(re.sub(r"(.)\1+", r"\1", word[0] + re.sub(r"[aeiouy]", "", word[1:])))
    return " ".join(keys)


def _artist_matches(wanted: str, artists: tuple[str, ...]) -> bool:
    wanted = _norm(wanted)
    for artist in map(_norm, artists):
        if wanted == artist or SequenceMatcher(None, wanted, artist).ratio() >= 0.8:
            return True
        if min(len(wanted), len(artist)) >= 3 and (wanted in artist or artist in wanted):
            return True
        if _sound(wanted) == _sound(artist):
            return True
    return False


def _title_points(name: str, wanted: str) -> float:
    """How well a title fits. Close counts as the same: Spotify's order already
    puts the famous song first, and preferring the exact wording picked an
    unknown "It's A Hard Knock Life" over "It's the Hard-Knock Life"."""
    name_words, wanted_words = set(name.split()), set(wanted.split())
    if name == wanted or SequenceMatcher(None, name, wanted).ratio() >= 0.8:
        return 4.0
    if name_words and (name_words <= wanted_words or wanted_words <= name_words):
        return 3.0              # "circles" in "circles post malone", and back
    return 2.0 * len(name_words & wanted_words) / max(1, len(name_words | wanted_words))


def score(item: Item, title: str, artist: str | None, asked: str) -> float:
    """How well `item` fits a request for `title` (by `artist`)."""
    full, wanted = _norm(item.name), _norm(title)
    # The bare title ("Circles") and the whole name ("Circles - Acoustic").
    base = _norm(_base(item.name))
    points = max(_title_points(base, wanted), _title_points(full, wanted))
    if artist:
        points += 2.0 if _artist_matches(artist, item.artists) else 0.0
    else:
        # The artist said in the query, beside the title ("circles post
        # malone"), not as the title itself ("Popcorn" by Popcorn).
        extra = set(wanted.split()) - set(base.split())
        if any(set(_norm(a).split()) and set(_norm(a).split()) <= extra for a in item.artists):
            points += 1.5
    asked = _norm(asked)
    if any(_has_word(v, full) and not _has_word(v, asked) for v in _VERSIONS):
        points -= 3.0
    return points


def best(items: list[Item], query: str) -> Item | None:
    """The item that best fits `query`; Spotify's order breaks ties."""
    if not items:
        return None
    title, artist = split_artist(query)

    def fit(item: Item) -> float:
        points = score(item, query, None, query)
        # Reading "X by Y" as title X by artist Y only counts when Y is one of
        # its artists: "Stand by Me" is not a song "Stand" by "Me".
        if artist and _artist_matches(artist, item.artists):
            points = max(points, score(item, title, artist, query))
        return points

    return max(enumerate(items), key=lambda pair: (fit(pair[1]), -pair[0]))[1]


def _to_item(kind: str, raw: dict | None) -> Item | None:
    if not raw or not raw.get("uri"):
        return None
    if kind == "playlist":
        owner = (raw.get("owner") or {}).get("display_name")
        artists = (owner,) if owner else ()
    else:
        artists = tuple(a.get("name", "") for a in raw.get("artists") or [] if a)
    return Item(kind, raw["uri"], raw.get("name", ""), artists if kind != "artist" else ())


def _search(q: str, kind: str) -> list[Item]:
    data = _api("GET", "/search", params={"q": q, "type": kind, "limit": 10}) or {}
    raws = (data.get(kind + "s") or {}).get("items") or []
    return [item for item in (_to_item(kind, r) for r in raws) if item]


def _abbreviates(name: str, said: str) -> bool:
    """Is `name` how `said` is written for short? "idek bro ig" for "I don't
    even know bro I guess": each word of the name is a word he said or the
    initials of the next few. Spelled out letter by letter ("I d e k b r o I
    g") counts too."""
    tokens, words = name.split(), said.split()
    if not tokens or not words:
        return False
    if "".join(tokens) == "".join(words):
        return True

    def fits(i: int, j: int) -> bool:
        if i == len(tokens):
            return j == len(words)
        token = tokens[i]
        if j < len(words) and words[j] == token and fits(i + 1, j + 1):
            return True
        k = len(token)
        return (k >= 2 and j + k <= len(words)
                and "".join(w[0] for w in words[j:j + k]) == token and fits(i + 1, j + k))

    return fits(0, 0)


def _my_playlist(query: str) -> Item | None:
    """One of his own or followed playlists with this name ("Night Car")."""
    data = _api("GET", "/me/playlists", params={"limit": 50}) or {}
    wanted = _norm(re.sub(r"\s*\bplaylist\b\s*", " ", query, flags=re.I))
    scored = []
    for raw in data.get("items") or []:
        item = _to_item("playlist", raw)
        if item:
            name = _norm(item.name)
            ratio = 1.0 if _abbreviates(name, wanted) else SequenceMatcher(None, wanted, name).ratio()
            scored.append((ratio, item))
    if not scored:
        return None
    ratio, item = max(scored, key=lambda pair: pair[0])
    return item if ratio >= 0.8 else None


def find(query: str, kind: str = "track") -> Item | None:
    """The best match for `query` among Spotify's `kind`s, or None."""
    query = re.sub(r"^\s*play\s+", "", query.strip(), flags=re.I)
    query = re.sub(r"\s+on\s+spotify\s*$", "", query, flags=re.I).strip()
    if not query:
        return None
    if kind == "playlist":
        mine = _my_playlist(query)
        if mine:
            return mine
    title, artist = split_artist(query)
    queries = [query]
    if artist and kind in ("track", "album"):
        # Field filters find "Circles" by Post Malone among every "Circles";
        # the plain query still covers a title with "by" in it.
        queries.append(f'{kind}:"{title}" artist:"{artist}"')
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(lambda q: _search(q, kind), queries))
    merged: dict[str, Item] = {}
    for item in (i for batch in results for i in batch):
        merged.setdefault(item.uri, item)
    return best(list(merged.values()), query)


# --- playing ---------------------------------------------------------------

def _devices() -> list[dict]:
    return (_api("GET", "/me/player/devices") or {}).get("devices") or []


def _this_computer(devices: list[dict]) -> dict | None:
    computers = [d for d in devices if d.get("type") == "Computer" and d.get("id")]
    host = socket.gethostname().lower()
    return (next((d for d in computers if (d.get("name") or "").lower() == host), None)
            or (computers[0] if computers else None))


def _open_desktop_app() -> None:
    from friday import plat
    plat.open_target("spotify:")


def device_id() -> str:
    """The device to play on: Spotify on this computer, started if it isn't
    running; failing that, whichever device is active."""
    devices = _devices()
    here = _this_computer(devices)
    if here:
        return here["id"]
    _open_desktop_app()
    deadline = time.monotonic() + _DEVICE_WAIT
    while time.monotonic() < deadline:
        time.sleep(0.7)
        devices = _devices()
        here = _this_computer(devices)
        if here:
            return here["id"]
    active = next((d for d in devices if d.get("is_active") and d.get("id")), None)
    if active:
        return active["id"]
    raise SpotifyError("Spotify didn't open on any device.")


def play(item: Item) -> None:
    body = {"uris": [item.uri]} if item.kind == "track" else {"context_uri": item.uri}
    target = device_id()
    try:
        _api("PUT", "/me/player/play", params={"device_id": target}, body=body)
    except SpotifyError:
        # A desktop app that just appeared can refuse its first command.
        time.sleep(1.0)
        _api("PUT", "/me/player/play", params={"device_id": target}, body=body)


def queue(item: Item) -> None:
    _api("POST", "/me/player/queue", params={"uri": item.uri})


def now_playing() -> tuple[Item, bool] | None:
    """(what's loaded, whether it's playing), or None when nothing is."""
    data = _api("GET", "/me/player/currently-playing")
    if not data or not data.get("item"):
        return None
    raw = data["item"]
    kind = "track" if raw.get("type") == "track" else raw.get("type", "track")
    item = _to_item(kind, raw)
    return (item, bool(data.get("is_playing"))) if item else None


def like(item: Item) -> None:
    """Save to his library (Liked Songs, for a track)."""
    _api("PUT", "/me/library", params={"uris": item.uri})


def main(argv: list[str] | None = None) -> int:
    import sys
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["login"]:
        try:
            login()
        except SpotifyError as e:
            print(e)
            return 1
        print("Signed in. Token saved to", TOKEN_FILE)
        return 0
    if args[:1] == ["status"]:
        if not ready():
            print("Not signed in." if client_id() else "SPOTIFY_CLIENT_ID is not set in .env.")
            return 1
        playing = now_playing()
        print("Signed in.", f"Now: {playing[0].label()}" if playing else "Nothing playing.")
        for d in _devices():
            print(f"  device: {d.get('name')} ({d.get('type')})"
                  + (" [active]" if d.get("is_active") else ""))
        return 0
    print("usage: python -m friday.spotify login|status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
