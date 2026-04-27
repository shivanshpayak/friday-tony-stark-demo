"""Data model for scheduled items (timers + reminders)."""
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class ScheduledItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    kind: Literal["timer", "reminder"]
    fire_at: str  # ISO 8601 local timestamp
    message: str  # what Jarvis should say when this fires
    label: str = ""  # optional user-friendly handle for cancellation
    created_at: str
