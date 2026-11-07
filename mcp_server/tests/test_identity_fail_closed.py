"""Current Switch MCP transport has no verified caller identity."""

from __future__ import annotations

import unittest

from mcp_server.server import get_pending_clarifications, respond_to_clarification


EXPECTED = {
    "ok": False,
    "error": {
        "code": "identity_unavailable",
        "message": (
            "Switch did not provide a verifiable LedgerRoom user identity. "
            "No backend request was made."
        ),
    },
}


class IdentityFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_clarifications_fails_closed_without_identity(self) -> None:
        self.assertEqual(await get_pending_clarifications(), EXPECTED)

    async def test_response_arguments_cannot_assert_identity(self) -> None:
        # Even identity-shaped strings supplied alongside ordinary tool inputs
        # cannot authorize an operation; the schema has no identity fields.
        result = await respond_to_clarification(
            "00000000-0000-0000-0000-000000000000", "Owner-provided explanation"
        )
        self.assertEqual(result, EXPECTED)
        self.assertNotIn("token", str(result).lower())
        self.assertNotIn("user_id", str(result).lower())


if __name__ == "__main__":
    unittest.main()
