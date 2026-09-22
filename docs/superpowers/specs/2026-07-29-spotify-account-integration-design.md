# Spotify Account Integration — Design

**Date:** 2026-07-29
**Status:** Approved, ready for implementation plan

## Problem

FRIDAY has no knowledge that the user has a Spotify account. The
`search_spotify` tool in
[friday/tools/media.py](../../../friday/tools/media.py) resolves every music
request by scraping DuckDuckGo:

```
DDGS().text(f"{query} site:open.spotify.com/{type}")  →  first matching URL
      →  regex out the ID  →  open spotify:{type}:{id}  →  sleep 2.5s
      →  send the play/pause media key
```

Consequences:

1. **Wrong content.** "Play my gym playlist" returns whichever public playlist
   named "gym" ranks first on DuckDuckGo. The user's own library is never
   consulted because FRIDAY has never authenticated to it.
2. **Slow.** A DDGS text search costs roughly 1–2s in the reply path, which
   conflicts with the latency-first rule in CLAUDE.md.
3. **Fragile.** Results depend on a third-party search index. A dead or
   redirected link yields a Spotify URI that navigates nowhere.
4. **Silent failure on cold start.** The fixed `time.sleep(2.5)` before the
   media key assumes Spotify is already running. When it is closed, the app has
   not finished launching at 2.5s, the keystroke goes nowhere, and the tool
   still returns `"Found the playlist and started playing it."` — violating the
   "never claim an action succeeded" rule in the system prompt.

## Constraint: the account is Spotify Free

Spotify's Web API rejects the player **control** endpoints
(`PUT /v1/me/player/play`, pause, next, seek, volume, shuffle, queue) with HTTP
403 for non-Premium accounts. This design therefore does **not** use them.
Launching stays exactly as it is today — a `spotify:` URI handed to the Windows
shell, followed by a media key.

What Free *does* allow is every read endpoint this feature actually depends on:
`/v1/me/playlists`, `/v1/me/tracks`, `/v1/me/albums`, and `/v1/search`. The fix
lives entirely in **resolution** — working out *which* URI to open — not in
playback.

If the account is ever upgraded to Premium, the launch path becomes a single
`start_playback(context_uri=...)` call and the media-key polling described below
can be deleted. Nothing else in this design changes.

## Module boundary

```
friday/spotify/
  auth.py       get_client() -> spotipy.Spotify | None
  library.py    LibraryCache — fetch, index, match, persist
friday/tools/spotify.py    MCP tool registrations
friday/tools/media.py      loses search_spotify; keeps volume, transport, current_track
setup_spotify.py           one-time browser consent, sibling to enroll_voice.py
```

Spotify moves out of `media.py` because the two concerns have diverged. `media.py`
is synchronous ctypes/pycaw against local Windows audio with no failure mode more
complex than "no audio session found." This feature is networked, stateful,
cached, token-authenticated, and rate-limited. Splitting returns `media.py` to
roughly 150 lines of pure audio control.

`friday/spotify/` is a package rather than a single module so that the auth
plumbing and the library cache can be tested independently — the cache logic is
pure and deserves tests that never touch OAuth.

## Authentication

`spotipy` handles the authorization-code flow: it opens the browser, runs the
localhost callback listener, caches the token, and refreshes silently on expiry.
This mirrors the existing precedent in
[friday/tools/google_suite.py](../../../friday/tools/google_suite.py), which uses
`google-auth-oauthlib` rather than hand-rolling OAuth.

- **Scopes:** `playlist-read-private`, `playlist-read-collaborative`,
  `user-library-read`. All read-only. No modify or playback scope is requested,
  so no code path in this feature is capable of altering the user's account.
- **Credentials:** `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`,
  read through `friday/config.py` alongside the other provider settings.
- **Redirect URI:** `http://127.0.0.1:8888/callback`, registered in the Spotify
  developer app.
- **Token cache:** `runtime/spotify_token.json`, matching where the rest of
  FRIDAY's mutable state lives.

`get_client()` returns `None` rather than raising when credentials are absent or
the refresh token is dead. Callers turn that into a spoken message.

`setup_spotify.py` performs the one-time consent so the browser window never
appears mid-conversation. It is a sibling to `enroll_voice.py` and follows the
same "run once during setup" convention.

## Library cache

`runtime/spotify_library.json`:

```json
{
  "version": 1,
  "fetched_at": "2026-07-29T14:02:11Z",
  "entries": [
    {"name": "Gym", "normalized": "gym", "uri": "spotify:playlist:37i9...",
     "kind": "playlist", "owner": "shivansh"}
  ]
}
```

Indexed sources, per the approved scope: **playlists** (`/me/playlists`, created
and followed), **liked songs** (`/me/tracks`), and **saved albums**
(`/me/albums`). Followed artists are deliberately excluded — they add
disambiguation value only, and every entry widens the fuzzy-match surface.

- Playlists and albums paginate at 50 per page; all pages are fetched.
- Liked songs are capped at the first 1000 to bound the boot fetch. Liked Songs
  as a whole is also indexed as a single synthetic entry
  (`spotify:collection:tracks`) so "play my liked songs" resolves directly.
- `version` allows a future schema change to invalidate stale caches rather than
  crash on them.

**Warming.** A daemon thread started in `register()` fetches the cache at MCP
server boot. Because it is a daemon thread it cannot block tool registration and
therefore cannot delay `FRIDAY_READY`. If the fetch fails, the previous on-disk
cache stays in use and the failure is logged, not spoken.

## Resolution ladder

`play_on_spotify("my gym playlist")` proceeds in strict order:

1. **Normalize.** Lowercase, strip punctuation, and drop filler tokens (`my`,
   `the`, `a`, `playlist`, `album`, `song`, `track`, `on spotify`).
2. **Match against the cache** using exact → token-subset → `difflib` ratio
   ≥ 0.8. This is the same ladder
   [friday/tools/apps.py](../../../friday/tools/apps.py) already uses for app
   name resolution; reusing it keeps fuzzy-match behavior consistent across
   FRIDAY rather than inventing a second dialect of "close enough."
3. **Hit** → return the cached URI. Zero network calls.
4. **Miss** → refresh the cache once, then retry the match. This covers the
   playlist created on a phone after boot, at the cost of ~300ms on that one
   miss. The refresh is attempted at most once per tool call.
5. **Still a miss** → `GET /v1/search` (`limit=1`, type inferred from `kind`),
   take the top result. Roughly 200ms, and it is Spotify's own ranked index
   rather than a scraped one.
6. **API unreachable** → open `spotify:search:<query>` and say so plainly:
   "Couldn't reach Spotify, sir — opened the search for you."

When `kind="auto"` (the default), a cache hit determines its own type from the
matched entry, and a catalog search queries `track,album,playlist` and takes the
highest-ranked result across all three.

Ties within the cache resolve in the order playlist → album → track, on the
reasoning that a user asking for a name that is both a playlist and a track
usually means the playlist.

## Launch path

Unchanged in mechanism, fixed in timing:

```
cmd /c start "" "<uri>"
→ poll _get_spotify_window_title() every 250ms, up to 8s
→ once the title leaves the idle set, send the play/pause media key
```

The existing idle-title set (`Spotify`, `Spotify Free`, `Spotify Premium`) in
`_get_spotify_window_title` already distinguishes "running but idle" from
"playing," so the poll needs no new detection logic. Replacing the fixed 2.5s
sleep fixes the cold-start case; the 8s ceiling prevents an unbounded thread if
Spotify never starts.

If the poll times out, the tool reports the truth: "Opened it, sir, but Spotify
didn't come up in time — playback may need a nudge." No success claim is made
for an unverified action.

The poll runs on a daemon thread so the tool returns immediately and FRIDAY's
spoken acknowledgement is not delayed by app launch.

## Tool surface

Three tools in `friday/tools/spotify.py`:

| Tool | Purpose |
|------|---------|
| `play_on_spotify(query, kind="auto")` | The only playback entry point |
| `list_my_playlists()` | Answers "what playlists do I have" from cache |
| `refresh_spotify_library()` | Manual re-sync |

`kind` accepts exactly `"auto"` (default), `"playlist"`, `"album"`, or
`"track"`. An unrecognized value is treated as `"auto"` rather than erroring —
the model occasionally invents enum values, and degrading to the default is
better than failing a playback request over it. An explicit `kind` filters both
the cache match and the catalog search to that type.

`play_on_spotify` is deliberately the sole play tool. Exposing separate
"play from my library" and "search and play" tools would force the model to
choose between two near-identical descriptions, which is a reliable source of
wrong-tool selection. The library-first preference is a property of the
implementation, not a decision delegated to the LLM.

`search_spotify` is **removed**, not deprecated. Leaving it registered means the
router can still select the scraper, which would reintroduce the original bug
intermittently and make it look non-deterministic.

`current_track` stays in `media.py` reading the window title: no network, no
auth, no Premium requirement, and it works even when playback was started
outside FRIDAY.

The `media` domain in
[friday/routing/domains.py](../../../friday/routing/domains.py) already carries
the keywords `play`, `playlist`, `album`, `spotify`, `music`, so routing needs no
change.

## Error handling

| Condition | Behavior |
|-----------|----------|
| No client ID/secret configured | "Sir, Spotify isn't linked. Run setup_spotify.py." |
| Token missing or refresh fails | Same message; token file is deleted so setup starts clean |
| HTTP 429 | Honor `Retry-After`; serve the stale cache instead of failing |
| HTTP 5xx or network error | Fall through to step 6 of the ladder |
| Cache file corrupt or wrong `version` | Discard, refetch, log; never crash the MCP server |
| Spotify app not installed | The `start` call fails; report it rather than claiming success |

The credential messages match the phrasing pattern already used in
`google_suite.py` for its missing `credentials.json`.

## Testing

`tests/test_spotify_library.py` — the matcher is a pure function over a list of
entries, so it is tested directly:

- normalization strips filler and punctuation as specified
- exact match wins over token-subset, which wins over fuzzy
- fuzzy matches below 0.8 are rejected rather than returned
- playlist → album → track tie ordering holds
- cache round-trips through JSON; a corrupt or wrong-version file is discarded

`tests/test_spotify_resolution.py` — the ladder against a fake client, asserting
ordering guarantees rather than implementation details:

- a library hit performs no search call
- a miss refreshes exactly once, then searches
- a second miss in the same call does not refresh again
- a search failure produces the `spotify:search:` fallback, not an exception

Both suites run without network access or credentials. The launch path (shell
invocation and media key) is not unit-tested — it is verified manually, since
mocking `subprocess` and `keyboard` would test the mocks rather than the
behavior.

## Documentation updates

Per the standing rule in CLAUDE.md, in the same change:

- **ARCHITECTURE.md** — feature/tool map gains the Spotify tools; the process
  diagram gains `friday/spotify/` and its two runtime files.
- **MCP_SERVERS_REFERENCE.md** (line ~193) — currently claims "Spotify playback
  + playlists + volume control" under "What FRIDAY already has," which
  overstates a DuckDuckGo scraper. Correct it.
- **CAPABILITY_PLAN.md** (line ~61) — the "Spotify — current-track query, queue
  management" depth item is mostly closed by this work. Queue management remains
  open and is Premium-gated, so it should be re-scoped rather than deleted.
- **pyproject.toml** — add `spotipy`.

## Out of scope

- **Queue management** (`add_to_queue`) — requires Premium.
- **Device targeting** ("play in the living room") — requires Premium.
- **Playlist modification** — no write scopes are requested, by design.
- **Semantic/vector matching over playlist names** — the fuzzy ladder is
  sufficient for a personal library of realistic size. Revisit only if
  string matching demonstrably fails.
