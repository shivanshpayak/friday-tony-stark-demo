from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

from livekit.agents.llm import Toolset
from livekit.agents.llm.mcp import MCPServerStdio, MCPToolset
from livekit.agents.llm.tool_context import (
    get_function_info,
    get_raw_function_info,
    is_raw_function_tool,
)

from friday.routing.domains import DOMAINS


def _tool_name(t) -> str:
    if is_raw_function_tool(t):
        return get_raw_function_info(t).name
    return get_function_info(t).name


# Maps each domain to the tool names it owns. Built from the same
# DOMAIN_MODULES registry that server.py uses, so they can never drift.
# Built lazily on first use: it imports and registers every tool module, which
# cost the agent ~1.8s at startup for a map nothing reads during boot.
@functools.cache
def _build_domain_tool_map() -> dict[str, set[str]]:
    from mcp.server.fastmcp import FastMCP
    from friday.tools import DOMAIN_MODULES

    mapping: dict[str, set[str]] = {}
    for domain, modules in DOMAIN_MODULES.items():
        mcp = FastMCP(name="probe")
        for mod in modules:
            mod.register(mcp)
        mapping[domain] = {t.name for t in mcp._tool_manager.list_tools()}
    return mapping


def __getattr__(name: str):
    if name == "DOMAIN_TOOL_NAMES":
        return _build_domain_tool_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _FilteredToolset(Toolset):
    """A lightweight read-only view over a subset of an MCPToolset's tools.

    This avoids mutating the shared MCPToolset — each LLM turn gets its own
    filtered view containing only the tools for the active domains.
    """

    def __init__(self, *, source: MCPToolset, allowed_names: set[str]) -> None:
        # Don't call super().__init__ with tools — we set _tools directly
        super().__init__(id=f"{source.id}-filtered")
        self._tools = [t for t in source.tools if _tool_name(t) in allowed_names]

    async def setup(self) -> _FilteredToolset:
        # Tools are already initialised on the source toolset — nothing to do.
        return self

    async def aclose(self) -> None:
        # Don't close — the source toolset owns the MCP connection.
        pass


class LocalDomainToolPool:
    """Single-process MCP tool pool with per-turn domain filtering.

    Instead of spawning one MCP server subprocess per domain (expensive in RAM),
    this spawns ONE server.py process with all tools loaded and filters the tool
    list per LLM turn based on the domain classifier output.
    """

    def __init__(self, *, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)
        self._toolset: MCPToolset | None = None
        self._setup_lock = asyncio.Lock()

    def _create_toolset(self) -> MCPToolset:
        return MCPToolset(
            id="jarvis-all",
            mcp_server=MCPServerStdio(
                command=sys.executable,
                args=[str(self._repo_root / "server.py")],
                cwd=str(self._repo_root),
                client_session_timeout_seconds=30,
            ),
        )

    async def _ensure_ready(self) -> MCPToolset:
        async with self._setup_lock:
            if self._toolset is None:
                self._toolset = self._create_toolset()
                await asyncio.shield(self._toolset.setup())
            return self._toolset

    def get_toolset(self, domain: str) -> MCPToolset:
        """Return the shared toolset (for pre-warming at boot).
        Actual domain filtering happens in get_toolsets()."""
        if domain not in DOMAINS:
            raise ValueError(f"Unknown routing domain: {domain!r}")
        if self._toolset is None:
            self._toolset = self._create_toolset()
        return self._toolset

    async def get_toolsets(self, domains: list[str]) -> list[_FilteredToolset]:
        """Return a filtered toolset containing only tools for the given domains."""
        source = await self._ensure_ready()

        # Collect all tool names from the requested domains
        allowed: set[str] = set()
        domain_tool_names = _build_domain_tool_map()
        for d in domains:
            names = domain_tool_names.get(d)
            if names:
                allowed |= names

        return [_FilteredToolset(source=source, allowed_names=allowed)]

    async def aclose(self) -> None:
        if self._toolset is not None:
            await self._toolset.aclose()
            self._toolset = None
