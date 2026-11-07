"""Request and response schemas for owner clarification workflows."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ClarificationStatus = Literal[
    "pending", "answered", "receipt_received", "resolved", "dismissed"
]


class ClarificationOwnerResponseRequest(BaseModel):
    owner_response: str = Field(min_length=1, max_length=10000)

    @field_validator("owner_response")
    @classmethod
    def require_nonblank_response(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("owner_response must not be blank.")
        return value


class ClarificationRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bank_line_id: str
    status: ClarificationStatus
    question: str
    owner_response: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    bank_description: str
    transaction_date: date
    amount_paise: int


class ClarificationRequestListResponse(BaseModel):
    clarifications: list[ClarificationRequestResponse] = Field(default_factory=list)
