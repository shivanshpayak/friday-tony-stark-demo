"""JSON persistence for scheduled items.

Single-file store — small payloads, single writer (the agent process).
"""
import json
import logging
from typing import Optional

from friday.config import SCHEDULED_STORE_PATH
from .models import ScheduledItem

logger = logging.getLogger("friday-agent")


def all_items() -> list[ScheduledItem]:
    if not SCHEDULED_STORE_PATH.is_file():
        return []
    try:
        data = json.loads(SCHEDULED_STORE_PATH.read_text(encoding="utf-8"))
        return [ScheduledItem(**item) for item in data]
    except Exception as e:
        logger.error("Failed to load scheduled items: %s", e)
        return []


def _write(items: list[ScheduledItem]) -> None:
    try:
        SCHEDULED_STORE_PATH.write_text(
            json.dumps([i.model_dump() for i in items], indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Failed to save scheduled items: %s", e)


def add(item: ScheduledItem) -> None:
    items = all_items()
    items.append(item)
    _write(items)


def remove(item_id: str) -> Optional[ScheduledItem]:
    items = all_items()
    for i in items:
        if i.id == item_id:
            items.remove(i)
            _write(items)
            return i
    return None


def find(label_or_id: str) -> Optional[ScheduledItem]:
    needle = label_or_id.strip().lower()
    for i in all_items():
        if i.id == label_or_id or (i.label and i.label.lower() == needle):
            return i
    return None
