"""Background scheduler — fires due items via the agent's TTS callback."""
import asyncio
import inspect
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional, Union

from friday.config import ALARM_MISSED_SECONDS, SCHEDULER_TICK_SECONDS
from .models import ScheduledItem
from .recurrence import next_occurrence
from .store import all_items, remove, update

logger = logging.getLogger("friday-agent")

FireCallback = Callable[[ScheduledItem], Union[None, Awaitable[None]]]

_callback: Optional[FireCallback] = None
_loop_task: Optional[asyncio.Task] = None


def set_fire_callback(cb: FireCallback) -> None:
    """Register the callback the agent uses to speak when an item fires."""
    global _callback
    _callback = cb


def collect_due(now: datetime) -> list[ScheduledItem]:
    """Take every item that has come due off the store; return those to fire.

    Taken off BEFORE firing so a slow callback can't double-fire. A repeating
    alarm goes back on for its next day instead, and an alarm that came due
    more than ALARM_MISSED_SECONDS ago (the laptop was asleep, JARVIS off) is
    skipped, not fired — "first period's over" at 5 PM helps nobody. Switched-
    off alarms are left alone.
    """
    due = []
    for item in all_items():
        if not item.enabled:
            continue
        try:
            fire_at = datetime.fromisoformat(item.fire_at)
        except Exception:
            logger.warning("Bad fire_at on %s: %r — discarding", item.id, item.fire_at)
            remove(item.id)
            continue
        if fire_at > now:
            continue
        if item.repeat:
            after = next_occurrence(fire_at.time(), item.repeat, now)
            update(item.model_copy(update={"fire_at": after.isoformat()}))
        else:
            remove(item.id)
        if item.kind == "alarm" and (now - fire_at).total_seconds() > ALARM_MISSED_SECONDS:
            logger.info("Alarm %s (%s) came due at %s while JARVIS was away — skipped",
                        item.id, item.label or "unlabelled", item.fire_at)
            continue
        due.append(item)
    return due


async def _tick_loop() -> None:
    while True:
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)
        try:
            for item in collect_due(datetime.now()):
                logger.info("Scheduler firing %s (%s): %s", item.id, item.kind, item.message)
                if _callback:
                    try:
                        result = _callback(item)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as e:
                        logger.error("Scheduler fire callback failed for %s: %s", item.id, e)
        except Exception as e:
            logger.error("Scheduler tick failed: %s", e)


def start_scheduler() -> None:
    """Start the background tick loop (idempotent)."""
    global _loop_task
    if _loop_task is None:
        _loop_task = asyncio.create_task(_tick_loop())
        logger.info("Scheduler started (tick=%.1fs)", SCHEDULER_TICK_SECONDS)
