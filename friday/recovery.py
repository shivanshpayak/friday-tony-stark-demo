"""Pure decision helpers for session-death recovery.

Kept dependency-free (no livekit/audio imports) so it is trivially unit-testable
and importable from both the agent and tests.
"""


def is_fatal_close(reason, *, intentional: bool) -> bool:
    """True when a session close should make the agent exit for a launcher respawn.

    `reason` is a livekit CloseReason (a str-Enum) or its string value. Only an
    unrecoverable ERROR close that we did NOT initiate ourselves is fatal.
    """
    value = getattr(reason, "value", reason)
    return value == "error" and not intentional


def should_announce_recovery(*, session_active: bool) -> bool:
    """True when the launcher should speak a recovery line after a respawn.

    Only announce when the drop happened mid-conversation; a death while idle
    (gated off, awaiting wake word) recovers silently back to wake-word listening.
    """
    return session_active
