"""Request and response schemas for invitations and accountant profiles."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import normalize_email


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        return normalize_email(value)


class InvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=256, repr=False)


class InvitationResponse(BaseModel):
    id: UUID
    business_id: UUID
    email: str
    role: str
    status: str
    expires_at: datetime
    created_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InvitationListResponse(BaseModel):
    invitations: list[InvitationResponse]


class InvitationCreatedResponse(BaseModel):
    invitation: InvitationResponse
    email_submission: str
    development_invitation_link: str | None = None
    shareable_invitation_link: str | None = None


class InvitationValidationResponse(BaseModel):
    invitation_id: UUID
    business_name: str
    invited_email: str
    expires_at: datetime
    message: str


class InvitationAcceptedResponse(BaseModel):
    invitation_id: UUID
    business_id: UUID
    membership_id: UUID
    role: str = "accountant"
    status: str = "active"
    mattermost_status: str | None = None
    message: str


class AccountantProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str = Field(min_length=8, max_length=32)
    firm_name: str | None = Field(default=None, max_length=255)
    office_address: str | None = Field(default=None, max_length=1000)
    professional_qualification: str = Field(min_length=1, max_length=255)
    professional_registration_number: str | None = Field(default=None, max_length=128)
    icai_membership_number: str | None = Field(default=None, max_length=64)

    @field_validator("full_name", "firm_name", "office_address", "professional_qualification", "professional_registration_number", "icai_membership_number")
    @classmethod
    def strip_optional_or_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This value cannot be empty.")
        return cleaned

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = re.sub(r"[\s().-]", "", value)
        digits = cleaned[1:] if cleaned.startswith("+") else cleaned
        if not digits.isascii() or not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise ValueError("Enter a valid phone number with 8 to 15 digits.")
        return cleaned


class AccountantProfileResponse(BaseModel):
    user_id: UUID
    full_name: str
    email: str
    phone: str
    firm_name: str | None
    office_address: str | None
    professional_qualification: str
    professional_registration_number: str | None
    icai_membership_number: str | None
    verification_status: str
    verification_note: str = "Self-reported; LedgerRoom has not verified these credentials."
    created_at: datetime
    updated_at: datetime
