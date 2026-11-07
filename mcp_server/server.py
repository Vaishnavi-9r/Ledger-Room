"""Local MCP tools for LedgerRoom.

Switch's current Codex MCP integration does not provide a verified Slack caller
identity to stdio tool handlers. Financial operations therefore fail closed
until a trusted identity bridge is available.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

server = MCPServer("LedgerRoom")


def _identity_unavailable() -> dict[str, Any]:
    """Return a safe failure without making an unauthenticated API request."""
    return {
        "ok": False,
        "error": {
            "code": "identity_unavailable",
            "message": (
                "Switch did not provide a verifiable LedgerRoom user identity. "
                "No backend request was made."
            ),
        },
    }


@server.tool()
async def get_pending_clarifications() -> dict[str, Any]:
    """List pending requests with request ID, bank description, date, paise amount, and status."""
    return _identity_unavailable()


@server.tool()
async def respond_to_clarification(
    request_id: str, owner_response: str
) -> dict[str, Any]:
    """Save the shop owner's explanation to a pending clarification request.

    This records the response only. It does not mark an invoice paid, match a
    bank transaction, or resolve the clarification.
    """
    # Identity must come from a trusted Switch integration, never these tool
    # arguments. The current runtime has no such identity channel.
    return _identity_unavailable()


if __name__ == "__main__":
    server.run(transport="stdio")
