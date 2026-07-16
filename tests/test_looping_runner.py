"""Tests for the pure loop-until-success engine."""
import threading

from friday.looping.runner import run_loop


class FakeClock:
    """Deterministic clock; the fake sleep advances it so time-based bounds fire."""
    def __init__(self):
        self.t = 0.0
    def time(self):
        return self.t
    def sleep(self, dt):
        self.t += dt


def _succeed_on(n):
    calls = {"n": 0}
    def check():
        calls["n"] += 1
        return calls["n"] >= n
    return check


def test_success_on_third_attempt():
    clk = FakeClock()
    r = run_loop(lambda: "ok", _succeed_on(3),
                 interval=1, max_attempts=10, max_seconds=1000,
                 clock=clk.time, sleep=clk.sleep)
    assert r.outcome == "success"
    assert r.attempts == 3


def test_exhausted_attempts():
    clk = FakeClock()
    r = run_loop(lambda: "ok", lambda: False,
                 interval=1, max_attempts=3, max_seconds=10_000,
                 clock=clk.time, sleep=clk.sleep)
    assert r.outcome == "exhausted_attempts"
    assert r.attempts == 3


def test_exhausted_time():
    clk = FakeClock()
    r = run_loop(lambda: "ok", lambda: False,
                 interval=1, max_attempts=1000, max_seconds=5,
                 clock=clk.time, sleep=clk.sleep)
    assert r.outcome == "exhausted_time"
    assert r.attempts == 5


def test_action_errors_tolerated_until_success():
    clk = FakeClock()
    state = {"n": 0}
    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise RuntimeError("transient")
    r = run_loop(flaky, lambda: state["n"] >= 2,
                 interval=1, max_attempts=10, max_seconds=1000,
                 clock=clk.time, sleep=clk.sleep)
    assert r.outcome == "success"


def test_three_consecutive_errors_abort():
    clk = FakeClock()
    def boom():
        raise OSError("adapter missing")
    r = run_loop(boom, lambda: True,
                 interval=1, max_attempts=100, max_seconds=1000,
                 clock=clk.time, sleep=clk.sleep)
    assert r.outcome == "aborted"
    assert r.attempts == 3
    assert "adapter missing" in r.reason


def test_cancel_stops_loop():
    clk = FakeClock()
    cancel = threading.Event()
    state = {"n": 0}
    def action():
        state["n"] += 1
        if state["n"] == 2:
            cancel.set()
    r = run_loop(action, lambda: False,
                 interval=1, max_attempts=100, max_seconds=1000,
                 clock=clk.time, sleep=clk.sleep, cancel_event=cancel)
    assert r.outcome == "cancelled"
    assert r.attempts == 2
