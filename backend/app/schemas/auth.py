"""Validated request and response schemas for authentication endpoints."""

from __future__ import annotations

import re
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(AuthRequest):
    full_name: str = Field(min_length=1, max_length=255)
    email: str = Field(max_length=320)
    mobile_number: str = Field(min_length=8, max_length=32)
    password: str = Field(min_length=12, max_length=128, repr=False)
    password_confirmation: str = Field(min_length=12, max_length=128, repr=False)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Full name is required.")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalize_email(value)
        return value.strip()

    @field_validator("mobile_number")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        cleaned = re.sub(r"[\s().-]", "", value)
        digits = cleaned[1:] if cleaned.startswith("+") else cleaned
        if not digits.isascii() or not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise ValueError("Enter a valid mobile number with 8 to 15 digits.")
        return cleaned

    @model_validator(mode="after")
    def passwords_must_match(self) -> Self:
        if self.password != self.password_confirmation:
            raise ValueError("Password confirmation does not match.")
        return self


class VerifyEmailRequest(AuthRequest):
    token: str = Field(min_length=32, max_length=256, repr=False)


class ResendVerificationRequest(AuthRequest):
    email: str = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalize_email(value)
        return value.strip()


class LoginRequest(AuthRequest):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalize_email(value)
        return value.strip()


class RefreshRequest(AuthRequest):
    refresh_token: str = Field(min_length=32, max_length=256, repr=False)


class MessageResponse(BaseModel):
    message: str


class RegistrationResponse(MessageResponse):
    email_verification_required: bool = True
    email_delivery_status: Literal["submitted", "development_link", "unavailable", "failed", "not_required"] = "unavailable"
    # Only returned by explicitly enabled local development mode and only for
    # loopback clients. Never logged or persisted as plain text.
    development_verification_link: str | None = None


class VerificationDeliveryResponse(MessageResponse):
    development_verification_link: str | None = None


class TokenResponse(BaseModel):
    token_type: str = "bearer"
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_in: int


class BusinessMembershipSummary(BaseModel):
    business_id: UUID
    role: str


class CurrentUserResponse(BaseModel):
    id: UUID
    full_name: str
    email: str
    mobile_number: str
    email_verified: bool
    memberships: list[BusinessMembershipSummary]
