"""
Tool registry — imports and registers tool modules with the MCP server.

The registry supports domain-scoped registration so the live voice agent can
keep a smaller tool surface per request while background executors can still
load the full suite.
"""

from collections.abc import Iterable

from friday.tools import (
    apps,
    audio,
    calculate,
    claude_delegate,
    clipboard,
    files,
    google_suite,
    maps,
    media,
    memory,
    messaging,
    scheduler,
    screen,
    sysmon,
    system,
    utils,
    weather,
    web,
    web_automation,
    network,
    frc,
    frc_tuner,
)

DOMAIN_MODULES = {
    # Core stays always-on for the live voice agent. It intentionally includes
    # the web/search tools because current-events questions are high-risk if the
    # model cannot reach search in the turn where it needs it.
    "core": (web, web_automation, system, utils, apps, messaging, memory, claude_delegate, network, weather, clipboard, sysmon, screen, scheduler, calculate, maps),
    "media": (media, audio),
    "files": (files,),
    "google": (google_suite,),
    "frc": (frc, frc_tuner),
}


def available_domains() -> tuple[str, ...]:
    return tuple(DOMAIN_MODULES.keys())


def register_all_tools(mcp, domains: Iterable[str] | None = None):
    """Register all requested tool groups onto the MCP server instance."""
    if domains is None:
        selected = tuple(DOMAIN_MODULES.keys())
    else:
        selected = tuple(dict.fromkeys(domains))

    for domain in selected:
        modules = DOMAIN_MODULES.get(domain)
        if modules is None:
            raise ValueError(
                f"Unknown tool domain: {domain!r}. Valid domains: {', '.join(available_domains())}"
            )
        for module in modules:
            module.register(mcp)
