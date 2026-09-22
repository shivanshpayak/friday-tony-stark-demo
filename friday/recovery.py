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


def respawn_backoff(failures: int) -> float:
    """Seconds to wait before the next agent (re)spawn after `failures`
    consecutive failed boots.

    0 → respawn immediately (normal recovery). Then 2, 4, 8, 16, capped at 30s,
    so an agent that dies on boot (bad code edit, no network, OS shutdown)
    can't spin the launcher into spawning processes several times a second.
    """
    if failures <= 0:
        return 0.0
    return float(min(2 ** failures, 30))


# An agent that stays up at least this long after FRIDAY_READY is considered a
# healthy boot; dying sooner (e.g. network down at boot → STT connect fails
# within seconds) counts as a failed respawn so the backoff keeps growing.
STABLE_UPTIME = 60.0


def next_respawn_failures(failures: int, *, uptime: float) -> int:
    """Consecutive-failure count to use for the respawn after an agent died
    `uptime` seconds after becoming ready."""
    return failures + 1 if uptime < STABLE_UPTIME else 0
