"""Pure loop-until-success engine. No network, no real time — fully injectable."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class LoopResult:
    outcome: str      # "success" | "exhausted_attempts" | "exhausted_time"
                      #  | "aborted" | "cancelled"
    attempts: int
    reason: str


def _wait(interval: float, sleep: Callable[[float], None],
          cancel_event: Optional[threading.Event]) -> None:
    """Sleep ~interval, but bail early (in slices) if cancellation is requested."""
    if interval <= 0:
        return
    if cancel_event is None:
        sleep(interval)
        return
    remaining = interval
    slice_ = 0.1
    while remaining > 0:
        if cancel_event.is_set():
            return
        step = slice_ if remaining > slice_ else remaining
        sleep(step)
        remaining -= step


def run_loop(
    action_fn: Callable[[], str],
    check_fn: Callable[[], bool],
    *,
    interval: float,
    max_attempts: int,
    max_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    cancel_event: Optional[threading.Event] = None,
    abort_after_consecutive_errors: int = 3,
) -> LoopResult:
    """Run action_fn, then check_fn, repeatedly until the check passes or a bound
    is hit. action_fn/check_fn may raise; N identical consecutive failures abort."""
    start = clock()
    attempts = 0
    consecutive_errors = 0
    last_error = ""

    while True:
        if cancel_event is not None and cancel_event.is_set():
            return LoopResult("cancelled", attempts, "Loop cancelled.")
        if clock() - start >= max_seconds:
            return LoopResult("exhausted_time", attempts,
                              last_error or f"No success within {int(max_seconds)}s.")
        if attempts >= max_attempts:
            return LoopResult("exhausted_attempts", attempts,
                              last_error or f"No success after {max_attempts} attempts.")

        attempts += 1
        errored = False
        try:
            action_fn()
        except Exception as e:
            errored = True
            consecutive_errors += 1
            last_error = f"{type(e).__name__}: {e}"
            if consecutive_errors >= abort_after_consecutive_errors:
                return LoopResult("aborted", attempts, last_error)

        if not errored:
            try:
                if check_fn():
                    return LoopResult("success", attempts, "Success condition met.")
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                last_error = f"{type(e).__name__}: {e}"
                if consecutive_errors >= abort_after_consecutive_errors:
                    return LoopResult("aborted", attempts, last_error)

        _wait(interval, sleep, cancel_event)
