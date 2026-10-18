"""Bass hits in whatever the laptop is playing, for the wallpaper to pulse to.

The Event Horizon wallpaper (~/event-horizon) flares its accretion disk on
every bass hit. QML can't capture audio, so this does: it reads the monitor
of the default output through parec, finds kick-like onsets in the 40-160 Hz
band, and serves them as a long poll on loopback:

    GET http://127.0.0.1:47812/music?after=<beat>
    -> {"beat": 41, "level": 0.62}

The answer comes the moment a beat newer than <beat> lands, or after 0.25 s
with just the current bass level (0..1). Audio is only read while something
is asking: capture stops 3 s after the last request, so a wallpaper hidden
behind a maximized window (which stops asking) costs nothing. The samples
never leave this process, and nothing is stored.

The launcher runs this as a subprocess (friday_overlay._SubprocessOverlay);
it exits when its stdin closes. Read-only, loopback only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

PORT = 47812
RATE = 16000
HOP = 256                 # 16 ms per analysis step
WINDOW = 1024             # 64 ms: enough resolution for 40-160 Hz
BASS_HZ = (40, 160)
_WAIT = 0.25              # longest a request waits without a beat
_IDLE = 3.0               # stop capturing this long after the last request
_RETRY = 2.0


class BassTracker:
    """Bass level and onsets from a stream of mono float samples in [-1, 1]."""

    SILENCE_RMS = 0.002       # about -54 dBFS: nothing playing
    MIN_GAP = 0.22            # s between beats: up to ~270 BPM
    HISTORY = 1.5             # s of bass energy the threshold adapts to
    LEVEL_RANGE = 18.0        # dB below the recent peak that still counts as some level

    def __init__(self):
        self.beat = 0
        self.level = 0.0
        self._buf = np.zeros(WINDOW, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._win = np.hanning(WINDOW).astype(np.float32)
        freqs = np.fft.rfftfreq(WINDOW, 1 / RATE)
        self._bins = (freqs >= BASS_HZ[0]) & (freqs <= BASS_HZ[1])
        self._history: deque[float] = deque(maxlen=int(self.HISTORY * RATE / HOP))
        self._prev_db = None
        self._peak_db = -120.0
        self._t = 0.0
        self._last_beat = -1.0

    def feed(self, samples: np.ndarray) -> bool:
        """Add samples; True if a new beat landed in them."""
        before = self.beat
        self._pending = np.concatenate([self._pending, samples.astype(np.float32)])
        while len(self._pending) >= HOP:
            hop, self._pending = self._pending[:HOP], self._pending[HOP:]
            self._step(hop)
        return self.beat != before

    def _step(self, hop: np.ndarray) -> None:
        self._t += HOP / RATE
        self._buf = np.concatenate([self._buf[HOP:], hop])
        if np.sqrt(np.mean(hop * hop)) < self.SILENCE_RMS:
            self.level = 0.0
            self._prev_db = None
            return
        spectrum = np.abs(np.fft.rfft(self._buf * self._win)) ** 2
        db = 10 * np.log10(spectrum[self._bins].sum() + 1e-12)

        self._peak_db = max(db, self._peak_db - 1.5 * HOP / RATE)   # forgets 1.5 dB/s
        self.level = float(np.clip(1 - (self._peak_db - db) / self.LEVEL_RANGE, 0, 1))

        if len(self._history) >= self._history.maxlen // 2 and self._prev_db is not None:
            mean = float(np.mean(self._history))
            spread = float(np.std(self._history))
            rising = db - self._prev_db > 1.0
            if (rising and db > mean + max(3.0, 1.5 * spread)
                    and self._t - self._last_beat >= self.MIN_GAP):
                self.beat += 1
                self._last_beat = self._t
        self._history.append(db)
        self._prev_db = db


class MusicPulse:
    """Capture on demand + the long poll around a BassTracker."""

    def __init__(self, port: int = PORT, wait: float = _WAIT, idle: float = _IDLE,
                 retry: float = _RETRY, capture_cmd=None):
        self.tracker = BassTracker()
        self._cond = threading.Condition()
        self._port, self._wait, self._idle, self._retry = port, wait, idle, retry
        self._capture_cmd = capture_cmd or _parec
        self._last_request = 0.0
        self._proc: subprocess.Popen | None = None
        self._stopping = threading.Event()
        self._listening = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        server = self._server
        return server.server_address[1] if server else self._port

    @property
    def capturing(self) -> bool:
        return self._proc is not None

    def listening(self, timeout: float | None = None) -> bool:
        return self._listening.wait(timeout)

    # -- the long poll ------------------------------------------------------

    def next_after(self, beat: int) -> tuple[int, float]:
        """(beat, level) once a beat newer than `beat` lands, or after a short
        wait with the current level. Asking is what keeps capture running."""
        with self._cond:
            self._last_request = time.monotonic()
            self._cond.notify_all()             # wakes the capture manager
            self._cond.wait_for(lambda: self.tracker.beat > beat, timeout=self._wait)
            return self.tracker.beat, round(self.tracker.level, 3)

    # -- capture ------------------------------------------------------------

    def _manage_capture(self) -> None:
        """Run parec while requests keep coming; stop it when they don't."""
        while not self._stopping.is_set():
            with self._cond:
                wanted = time.monotonic() - self._last_request < self._idle
            if wanted and self._proc is None:
                self._start_capture()
            elif not wanted and self._proc is not None:
                self._stop_capture()
            with self._cond:
                self._cond.wait(timeout=0.5)

    def _start_capture(self) -> None:
        try:
            self._proc = subprocess.Popen(self._capture_cmd(), stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
        except OSError:
            self._proc = None
            return
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()

    def _stop_capture(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._cond:
            self.tracker.level = 0.0

    def _read(self, proc: subprocess.Popen) -> None:
        while True:
            chunk = proc.stdout.read(HOP * 2)
            if not chunk:
                break
            samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2") / 32768.0
            with self._cond:
                if self.tracker.feed(samples):
                    self._cond.notify_all()
        if self._proc is proc:          # died by itself (output device gone): let it restart
            self._proc = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._stopping.clear()
        threading.Thread(target=self._serve, name="music-pulse", daemon=True).start()
        threading.Thread(target=self._manage_capture, name="music-capture", daemon=True).start()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self._port), _handler(self))
            except OSError:
                self._stopping.wait(self._retry)
                continue
            self._server = server
            self._listening.set()
            server.serve_forever(poll_interval=0.5)
            server.server_close()
            return

    def stop(self) -> None:
        self._stopping.set()
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
        self._stop_capture()
        with self._cond:
            self._cond.notify_all()


def _parec() -> list[str]:
    return ["parec", "--device=@DEFAULT_MONITOR@", "--format=s16le", f"--rate={RATE}",
            "--channels=1", "--latency-msec=20", "--raw"]


def _handler(pulse: MusicPulse):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/music":
                self.send_error(404)
                return
            try:
                after = int(parse_qs(url.query).get("after", ["-1"])[0])
            except ValueError:
                after = -1
            beat, level = pulse.next_after(after)
            body = json.dumps({"beat": beat, "level": level}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    return Handler


def main() -> None:
    pulse = MusicPulse()
    pulse.start()
    for _ in sys.stdin:     # nothing to read; EOF means the launcher is gone
        pass
    pulse.stop()


if __name__ == "__main__":
    main()
