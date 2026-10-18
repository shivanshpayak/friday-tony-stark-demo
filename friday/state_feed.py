"""The overlay state (listening, thinking, speaking, ...) for the desktop to react to.

The Event Horizon wallpaper (~/event-horizon) lights up its black hole when
JARVIS wakes, listens, thinks and talks. QML can't reach the launcher's Unix
control socket, but it can make HTTP requests, so the state is served as a
long poll on loopback:

    GET http://127.0.0.1:47811/state?after=<seq>
    -> {"seq": 12, "state": "listening"}

The answer comes as soon as the state is newer than <seq> (at once for a
first request, after=-1), or after 25 s with the state unchanged. Read-only,
loopback only. A port that's taken is retried every couple of seconds rather
than given up on: `friday_ctl restart` starts the new launcher while the old
one still holds it.

Deliberately free of any MCP/LiveKit import: the launcher loads this module.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 47811
_WAIT = 25.0
_RETRY = 2.0


class StateFeed:
    def __init__(self, port: int = PORT, wait: float = _WAIT, retry: float = _RETRY):
        self._cond = threading.Condition()
        self._seq = 0
        self._state = "sleeping"
        self._port = port
        self._wait = wait
        self._retry = retry
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listening = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        """The port actually listened on (useful when constructed with 0)."""
        server = self._server
        return server.server_address[1] if server else self._port

    def listening(self, timeout: float | None = None) -> bool:
        """Whether the feed is up, waiting up to `timeout` for it."""
        return self._listening.wait(timeout)

    def publish(self, state: str) -> None:
        with self._cond:
            if state == self._state:
                return
            self._state = state
            self._seq += 1
            self._cond.notify_all()

    def next_after(self, seq: int) -> tuple[int, str]:
        """(seq, state) once the state is newer than `seq`, or the current one
        when nothing changes for a while. Any seq the feed didn't hand out
        (a first request, or one from before a relaunch) answers at once."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq, timeout=self._wait)
            return self._seq, self._state

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="state-feed", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self._port), _handler(self))
            except OSError:
                self._stopping.wait(self._retry)
                continue
            with self._lock:
                if self._stopping.is_set():
                    server.server_close()
                    return
                self._server = server
            self._listening.set()
            server.serve_forever(poll_interval=0.5)
            server.server_close()
            return

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            server, self._server = self._server, None
        self._thread = None
        self._listening.clear()
        if server is not None:
            server.shutdown()       # seen by serve_forever even if it hasn't begun yet
        with self._cond:            # let any waiting request answer now
            self._cond.notify_all()


def _handler(feed: StateFeed):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/state":
                self.send_error(404)
                return
            try:
                after = int(parse_qs(url.query).get("after", ["-1"])[0])
            except ValueError:
                after = -1
            seq, state = feed.next_after(after)
            body = json.dumps({"seq": seq, "state": state}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass        # the wallpaper went away mid-wait

        def log_message(self, *args):
            pass

    return Handler
