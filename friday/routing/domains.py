from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDomain:
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    always_loaded: bool = False


DOMAINS: dict[str, ToolDomain] = {
    "core": ToolDomain(
        name="core",
        description="Apps, system actions, search, lightweight messaging, memory, and delegation",
        always_loaded=True,
    ),
    "files": ToolDomain(
        name="files",
        description="File and folder listing, reading, writing, moving, copying, and deleting",
        keywords=(
            "file",
            "files",
            "folder",
            "folders",
            "directory",
            "directories",
            "log",
            "logs",
            "rename",
            "move file",
            "copy file",
            "delete file",
            "delete folder",
            "read this file",
            "summarize this file",
            "write to",
            "create folder",
        ),
    ),
    "media": ToolDomain(
        name="media",
        description="Music playback, Spotify control, volume, and song recognition",
        keywords=(
            "play",
            "pause",
            "resume",
            "skip",
            "next track",
            "previous track",
            "song",
            "track",
            "playlist",
            "album",
            "spotify",
            "music",
            "volume",
            "shazam",
            "humming",
            "what's playing",
            "queue",
            "lyrics",
            "goes like",
            "liked songs",
        ),
    ),
    "google": ToolDomain(
        name="google",
        description="Calendar and inbox access",
        keywords=(
            "calendar",
            "event",
            "events",
            "meeting",
            "meetings",
            "schedule",
            "agenda",
            "reschedule",
            "book",
            "email",
            "emails",
            "inbox",
            "mail",
        ),
    ),
    "frc": ToolDomain(
        name="frc",
        description="FIRST Robotics Competition tools for building, deploying, and simulating code",
        keywords=(
            "frc",
            "robot",
            "robot code",
            "deploy",
            "build code",
            "simulation",
            "driver station",
            "dashboard",
            "elastic",
            "roborio",
        ),
    ),
    "phone": ToolDomain(
        name="phone",
        description="His phone over KDE Connect: ring it, battery, send it links, files and notes",
        keywords=(
            "phone",
            "iphone",
            "my phone",
            "find my phone",
            "ring my phone",
        ),
    ),
}


ALWAYS_LOADED_DOMAINS: tuple[str, ...] = tuple(
    name for name, domain in DOMAINS.items() if domain.always_loaded
)

OPTIONAL_DOMAINS: tuple[str, ...] = tuple(
    name for name, domain in DOMAINS.items() if not domain.always_loaded
)
