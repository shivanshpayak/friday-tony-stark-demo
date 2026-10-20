"""Phone tools — find it, send it things (friday/phone.py does the KDE Connect side)."""
from __future__ import annotations

from friday import phone, plat


def register(mcp):

    @mcp.tool(name="find_my_phone")
    def find_my_phone() -> str:
        """Put Find My on the screen, where Play Sound rings his phone wherever
        it is. Use for "find my phone", "where's my phone", "ring my phone"."""
        return phone.find_my_phone()

    if plat.IS_WINDOWS:
        return

    @mcp.tool(name="send_to_phone")
    def send_to_phone(content: str) -> str:
        """Send something to his phone. A link (http/https) opens on it; an
        absolute file path sends that file; anything else arrives as a
        notification, so a note or reminder to read later goes here as plain
        text. Use for "send this link to my phone", "send the report to my
        phone", "put 'buy milk' on my phone". Only send what he asked for."""
        return phone.send(content)
