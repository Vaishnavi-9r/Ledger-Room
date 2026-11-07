"""Validated, secret-safe client for the local LedgerRoom API."""

from __future__ import annotations

import ipaddress
import logging
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("ledgerroom.mcp")


class Clarification(BaseModel):
    """Public fields returned by the MCP tool; owner text is omitted."""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(strict=True, min_length=1)
    bank_line_id: str = Field(strict=True, min_length=1)
    description: str = Field(strict=True, min_length=1)
    transaction_date: date
    amount_paise: int = Field(strict=True, ge=0)
    status: Literal["pending", "answered", "receipt_received", "resolved", "dismissed"]


class OwnerResponseSubmission(BaseModel):
    """Validated inputs matching the existing clarification response API."""

    request_id: UUID
    owner_response: str = Field(strict=True, min_length=1, max_length=10000)

    @classmethod
    def validate_submission(cls, request_id: str, owner_response: str) -> "OwnerResponseSubmission":
        normalized = owner_response.strip() if isinstance(owner_response, str) else owner_response
        return cls.model_validate(
            {"request_id": request_id, "owner_response": normalized}
        )


class OwnerResponseResult(BaseModel):
    """Fields from ClarificationRequestResponse needed to confirm persistence."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    status: Literal["pending", "answered", "receipt_received", "resolved", "dismissed"]
    owner_response: str | None


class BackendError(Exception):
    """Safe error from reading the configured local LedgerRoom API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_backend_url(url: str) -> str:
    """Require a loopback HTTP URL so requests cannot target a public API."""
    parsed = urlsplit(url)
    try:
        host = parsed.hostname
        loopback = bool(host) and (
            host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        loopback = False
    if parsed.scheme != "http" or not loopback or parsed.username or parsed.password:
        raise BackendError(
            "invalid_configuration",
            "Backend URL must be a local loopback HTTP address, such as http://127.0.0.1:8010.",
        )
    return url.rstrip("/")


def parse_response(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("clarifications"), list):
        raise BackendError(
            "invalid_response", "Backend returned an unexpected clarification response."
        )
    result: list[dict[str, Any]] = []
    try:
        for row in payload["clarifications"]:
            if not isinstance(row, dict):
                raise ValueError("Clarification item must be an object")
            item = Clarification(
                request_id=row.get("id"),
                bank_line_id=row.get("bank_line_id"),
                description=row.get("bank_description"),
                transaction_date=row.get("transaction_date"),
                amount_paise=row.get("amount_paise"),
                status=row.get("status"),
            )
            if item.status == "pending":
                result.append(item.model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning("LedgerRoom returned an invalid clarification item (%s)", type(exc).__name__)
        raise BackendError(
            "invalid_response", "Backend returned an invalid clarification item."
        ) from exc
    return result


async def fetch_pending_clarifications(
    client: httpx.AsyncClient, backend_url: str
) -> list[dict[str, Any]]:
    """Fetch pending clarifications; kept separate for deterministic tests."""
    url = validate_backend_url(backend_url) + "/api/v1/clarifications/"
    try:
        response = await client.get(url, params={"status": "pending"})
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.warning("LedgerRoom API request timed out")
        raise BackendError("timeout", "LedgerRoom backend request timed out.") from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("LedgerRoom API returned HTTP %s", exc.response.status_code)
        raise BackendError(
            "backend_error", "LedgerRoom backend could not serve clarification requests."
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("LedgerRoom API is unavailable (%s)", type(exc).__name__)
        raise BackendError(
            "backend_unavailable", "LedgerRoom backend is unavailable. Start the local API and try again."
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("LedgerRoom API returned invalid JSON")
        raise BackendError("invalid_response", "Backend returned invalid JSON.") from exc
    return parse_response(payload)


async def submit_clarification_response(
    client: httpx.AsyncClient,
    backend_url: str,
    request_id: str,
    owner_response: str,
) -> dict[str, str | bool]:
    """Save an owner response through the existing pending-only API endpoint."""
    try:
        submission = OwnerResponseSubmission.validate_submission(request_id, owner_response)
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        logger.info("Rejected invalid clarification response input")
        raise BackendError(
            "invalid_input",
            "Provide a valid clarification UUID and a non-empty response of at most 10000 characters.",
        ) from exc

    url = (
        validate_backend_url(backend_url)
        + f"/api/v1/clarifications/{submission.request_id}/respond"
    )
    try:
        response = await client.post(
            url, json={"owner_response": submission.owner_response}
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.warning("LedgerRoom clarification response timed out")
        raise BackendError("timeout", "LedgerRoom backend request timed out.") from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.info("LedgerRoom clarification response returned HTTP %s", code)
        if code == 404:
            raise BackendError(
                "not_found", "Clarification request was not found."
            ) from exc
        if code == 409:
            raise BackendError(
                "conflict", "Clarification is no longer pending; the response was not saved."
            ) from exc
        raise BackendError(
            "backend_error", "LedgerRoom backend could not save the clarification response."
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("LedgerRoom API is unavailable (%s)", type(exc).__name__)
        raise BackendError(
            "backend_unavailable",
            "LedgerRoom backend is unavailable. Start the local API and try again.",
        ) from exc

    try:
        payload = response.json()
        result = OwnerResponseResult.model_validate(payload)
    except (ValueError, ValidationError, TypeError) as exc:
        logger.warning("LedgerRoom returned an invalid clarification response result")
        raise BackendError(
            "invalid_response", "Backend returned an invalid response confirmation."
        ) from exc

    if (
        result.id != submission.request_id
        or result.status != "answered"
        or result.owner_response != submission.owner_response
    ):
        logger.warning("LedgerRoom response confirmation did not match the submitted request")
        raise BackendError(
            "invalid_response", "Backend did not confirm the submitted response."
        )

    return {
        "ok": True,
        "message": "Owner response saved to the clarification request.",
        "request_id": str(result.id),
        "status": result.status,
    }
