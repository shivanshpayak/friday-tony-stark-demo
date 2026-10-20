"""Data model for scheduled items (timers, reminders, alarms)."""
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class ScheduledItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    kind: Literal["timer", "reminder", "alarm"]
    fire_at: str  # ISO 8601 local timestamp; an alarm's next ring
    message: str  # what Jarvis should say when this fires
    label: str = ""  # optional user-friendly handle for cancellation
    created_at: str
    # Alarms only. Weekdays it repeats on (0=Monday); empty rings once and is
    # gone. Off keeps it, like the switch in Windows Clock.
    repeat: list[int] = Field(default_factory=list)
    enabled: bool = True
