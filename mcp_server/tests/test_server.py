"""Tests for the MCP adapter's HTTP boundary; no real backend is contacted."""

from __future__ import annotations

import unittest

import httpx

from mcp_server.backend_client import (
    BackendError,
    fetch_pending_clarifications,
    submit_clarification_response,
)


class PendingClarificationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_requested_fields_and_filters_pending(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/clarifications/")
            self.assertEqual(request.url.params.get("status"), "pending")
            return httpx.Response(
                200,
                json={
                    "clarifications": [
                        {
                            "id": "clarification-1",
                            "bank_line_id": "bank-line-1",
                            "status": "pending",
                            "question": "May we have the receipt?",
                            "owner_response": None,
                            "created_at": "2026-11-05T10:00:00Z",
                            "updated_at": "2026-11-05T10:00:00Z",
                            "resolved_at": None,
                            "bank_description": "ABC STATIONERY",
                            "transaction_date": "2026-11-03",
                            "amount_paise": 250000,
                        },
                        {
                            "id": "clarification-answered",
                            "bank_line_id": "bank-line-answered",
                            "status": "answered",
                            "question": "Already handled?",
                            "owner_response": "Done",
                            "created_at": "2026-11-04T10:00:00Z",
                            "updated_at": "2026-11-04T11:00:00Z",
                            "resolved_at": None,
                            "bank_description": "OTHER STORE",
                            "transaction_date": "2026-11-02",
                            "amount_paise": 10000,
                        },
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_pending_clarifications(client, "http://127.0.0.1:8010")

        self.assertEqual(
            result,
            [
                {
                    "request_id": "clarification-1",
                    "bank_line_id": "bank-line-1",
                    "description": "ABC STATIONERY",
                    "transaction_date": "2026-11-03",
                    "amount_paise": 250000,
                    "status": "pending",
                }
            ],
        )

    async def test_backend_unavailable_is_safe(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("private connection detail")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(BackendError) as raised:
                await fetch_pending_clarifications(client, "http://127.0.0.1:8010")
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertNotIn("private connection detail", raised.exception.message)

    async def test_timeout_is_safe(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("private timeout detail")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(BackendError) as raised:
                await fetch_pending_clarifications(client, "http://127.0.0.1:8010")
        self.assertEqual(raised.exception.code, "timeout")
        self.assertNotIn("private timeout detail", raised.exception.message)

    async def test_http_and_invalid_response_errors_are_safe(self) -> None:
        async def call_with(response: httpx.Response) -> BackendError:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: response)
            ) as client:
                with self.assertRaises(BackendError) as raised:
                    await fetch_pending_clarifications(client, "http://127.0.0.1:8010")
            return raised.exception

        provider_error = await call_with(httpx.Response(503, text="private provider detail"))
        malformed = await call_with(httpx.Response(200, json={"unexpected": []}))
        self.assertEqual(provider_error.code, "backend_error")
        self.assertEqual(malformed.code, "invalid_response")
        self.assertNotIn("private provider detail", provider_error.message)

    async def test_public_backend_url_is_rejected(self) -> None:
        async with httpx.AsyncClient() as client:
            with self.assertRaises(BackendError) as raised:
                await fetch_pending_clarifications(client, "https://example.com")
        self.assertEqual(raised.exception.code, "invalid_configuration")


class RespondToClarificationToolTests(unittest.IsolatedAsyncioTestCase):
    request_id = "78532d14-65c6-4de7-9be2-7f8aa8c88127"
    backend_url = "http://127.0.0.1:8010"

    async def test_success_posts_owner_response_and_confirms_saved(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.path,
                f"/api/v1/clarifications/{self.request_id}/respond",
            )
            self.assertEqual(request.read(), b'{"owner_response":"Purchased for shop supplies."}')
            return httpx.Response(
                200,
                json={
                    "id": self.request_id,
                    "bank_line_id": "98b3f2a1-e29f-46fb-970b-ddfdc19ebbf9",
                    "status": "answered",
                    "question": "What was this debit for?",
                    "owner_response": "Purchased for shop supplies.",
                    "created_at": "2026-11-05T10:00:00Z",
                    "updated_at": "2026-11-05T11:00:00Z",
                    "resolved_at": None,
                    "bank_description": "ABC STATIONERY",
                    "transaction_date": "2026-11-03",
                    "amount_paise": 250000,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await submit_clarification_response(
                client,
                self.backend_url,
                self.request_id,
                "  Purchased for shop supplies.  ",
            )

        self.assertEqual(
            result,
            {
                "ok": True,
                "message": "Owner response saved to the clarification request.",
                "request_id": self.request_id,
                "status": "answered",
            },
        )

    async def test_invalid_uuid_is_rejected_without_backend_call(self) -> None:
        calls: list[httpx.Request] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(request))
        ) as client:
            with self.assertRaises(BackendError) as raised:
                await submit_clarification_response(
                    client, self.backend_url, "not-a-uuid", "Owner explanation"
                )
        self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(calls, [])

    async def test_empty_response_is_rejected_without_backend_call(self) -> None:
        calls: list[httpx.Request] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(request))
        ) as client:
            with self.assertRaises(BackendError) as raised:
                await submit_clarification_response(
                    client, self.backend_url, self.request_id, "   "
                )
        self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(calls, [])

    async def test_http_404_maps_to_not_found(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(404, text="private backend detail")
            )
        ) as client:
            with self.assertRaises(BackendError) as raised:
                await submit_clarification_response(
                    client, self.backend_url, self.request_id, "Owner explanation"
                )
        self.assertEqual(raised.exception.code, "not_found")
        self.assertNotIn("private backend detail", raised.exception.message)

    async def test_http_409_maps_to_pending_conflict(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(409, text="private backend detail")
            )
        ) as client:
            with self.assertRaises(BackendError) as raised:
                await submit_clarification_response(
                    client, self.backend_url, self.request_id, "Owner explanation"
                )
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIn("no longer pending", raised.exception.message)
        self.assertNotIn("private backend detail", raised.exception.message)

    async def test_backend_unavailable_is_safe(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("private connection detail")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(BackendError) as raised:
                await submit_clarification_response(
                    client, self.backend_url, self.request_id, "Owner explanation"
                )
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertNotIn("private connection detail", raised.exception.message)


if __name__ == "__main__":
    unittest.main()

