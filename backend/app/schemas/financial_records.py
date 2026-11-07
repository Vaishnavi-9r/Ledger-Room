"""Schemas for business-scoped expected vendors and event history."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpectedVendorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    active: bool = True

    @field_validator("name")
    @classmethod
    def name_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Vendor name is required.")
        return value

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        return cleaned


class ExpectedVendorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    aliases: list[str] | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    active: bool | None = None


class ExpectedVendorResponse(BaseModel):
    id: UUID
    name: str
    aliases: list[str]
    currency: str | None
    active: bool
    created_at: datetime


class ChaseEventResponse(BaseModel):
    id: UUID
    invoice_id: UUID | None
    bank_line_id: UUID | None
    match_id: UUID | None
    clarification_request_id: UUID | None
    event_type: str
    details: dict[str, object]
    created_at: datetime
