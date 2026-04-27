"""Background scheduler — fires due items via the agent's TTS callback."""
import asyncio
import inspect
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional, Union

from friday.config import SCHEDULER_TICK_SECONDS
from .models import ScheduledItem
from .store import all_items, remove

logger = logging.getLogger("friday-agent")

FireCallback = Callable[[ScheduledItem], Union[None, Awaitable[None]]]

_callback: Optional[FireCallback] = None
_loop_task: Optional[asyncio.Task] = None


def set_fire_callback(cb: FireCallback) -> None:
    """Register the callback the agent uses to speak when an item fires."""
    global _callback
    _callback = cb


async def _tick_loop() -> None:
    while True:
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)
        try:
            now = datetime.now()
            for item in all_items():
                try:
                    fire_at = datetime.fromisoformat(item.fire_at)
                except Exception:
                    logger.warning("Bad fire_at on %s: %r — discarding", item.id, item.fire_at)
                    remove(item.id)
                    continue

                if fire_at <= now:
                    logger.info("Scheduler firing %s (%s): %s", item.id, item.kind, item.message)
                    # Remove BEFORE firing so a slow callback can't double-fire.
                    remove(item.id)
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
