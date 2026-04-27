"""
JARVIS MCP Server — Entry Point
Default transport is stdio (used by agent_friday.py as an embedded subprocess).
Pass --sse to expose the same tools over SSE for external clients.

Run:
  python server.py                      # stdio (all tools)
  python server.py --domain core        # stdio (core tools only)
  python server.py --sse                # sse on default port
"""

import argparse

from mcp.server.fastmcp import FastMCP
from friday.tools import register_all_tools
from friday.prompts import register_all_prompts
from friday.resources import register_all_resources

# Create the MCP server instance
mcp = FastMCP(
    name="Jarvis",
    instructions=(
        "You are Jarvis, a personal AI assistant. "
        "You have access to a set of tools to help the user. "
        "Be concise, accurate, and direct."
    ),
)

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--sse", action="store_true")
    parser.add_argument("--streamable-http", action="store_true")
    parser.add_argument("--domain", action="append", default=[])
    return parser.parse_args()


def main():
    args = _parse_args()

    # Register tools, prompts, and resources.
    register_all_tools(mcp, domains=args.domain or None)
    register_all_prompts(mcp)
    register_all_resources(mcp)

    # Default transport is stdio so agent_friday.py can spawn this module
    # as an MCPServerStdio subprocess without any port / URL config.
    transport = "stdio"
    if args.sse:
        transport = "sse"
    elif args.streamable_http:
        transport = "streamable-http"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
