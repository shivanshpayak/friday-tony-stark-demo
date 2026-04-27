"""
Data resources — expose static content or dynamic data via MCP resources.
"""


def register(mcp):

    @mcp.resource("jarvis://info")
    def server_info() -> str:
        """Returns basic info about this MCP server."""
        return (
            "Jarvis MCP Server\n"
            "A personal AI assistant.\n"
            "Built with FastMCP."
        )
