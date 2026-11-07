"""Business registration, profile, and membership API schemas."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .auth import normalize_email

_GST_STATE_CODES = {f"{number:02d}" for number in range(1, 39)} | {"96", "97", "99"}
_GSTIN_PATTERN = re.compile(r"^([0-9]{2})[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GST_STATUS = {"unregistered", "pending", "registered", "not_applicable"}


def gstin_checksum_valid(gstin: str) -> bool:
    """Validate GSTIN structure and its mod-36 check character, not registration status."""
    match = _GSTIN_PATTERN.fullmatch(gstin)
    if match is None or match.group(1) not in _GST_STATE_CODES:
        return False
    factor = 1
    total = 0
    for char in gstin[:14]:
        product = factor * _BASE36.index(char)
        total += product // 36 + product % 36
        factor = 2 if factor == 1 else 1
    expected = _BASE36[(36 - total % 36) % 36]
    return gstin[-1] == expected


def normalize_gstin(value: str | None) -> str | None:
    if value is None:
        return None
    gstin = value.strip().upper()
    if not gstin:
        return None
    if not gstin_checksum_valid(gstin):
        raise ValueError("GSTIN format or checksum is invalid.")
    return gstin


def validate_phone(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[\s().-]", "", value)
    digits = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not digits.isascii() or not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise ValueError("Enter a valid phone number with 8 to 15 digits.")
    return cleaned


def validate_country(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise ValueError("Country must be a two-letter ISO country code.")
    return value


def _validate_status(gstin: str | None, status: str | None) -> str:
    value = (status or ("registered" if gstin else "unregistered")).strip().casefold()
    if value not in _GST_STATUS:
        raise ValueError("Invalid GST registration status.")
    if value == "registered" and not gstin:
        raise ValueError("GSTIN is required when GST registration status is registered.")
    if gstin and value in {"unregistered", "not_applicable"}:
        raise ValueError("GST registration status conflicts with the supplied GSTIN.")
    return value


class BusinessInputBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_name: str | None = Field(default=None, min_length=1, max_length=255)
    proprietor_name: str | None = Field(default=None, min_length=1, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    business_type: str | None = Field(default=None, min_length=1, max_length=64)
    business_email: str | None = Field(default=None, max_length=320)
    business_phone: str | None = Field(default=None, max_length=32)
    address_line_1: str | None = Field(default=None, min_length=1, max_length=255)
    address_line_2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=1, max_length=128)
    state: str | None = Field(default=None, min_length=1, max_length=128)
    pin_code: str | None = Field(default=None, min_length=1, max_length=16)
    country: str | None = None
    gst_registration_status: str | None = None

    @field_validator("business_name", "proprietor_name", "business_type", "address_line_1", "address_line_2", "city", "state", "pin_code")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be empty.")
        return value

    @field_validator("business_email")
    @classmethod
    def email_valid(cls, value: str | None) -> str | None:
        return normalize_email(value) if value is not None else None

    @field_validator("business_phone")
    @classmethod
    def phone_valid(cls, value: str | None) -> str | None:
        return validate_phone(value)

    @field_validator("gstin")
    @classmethod
    def gstin_valid(cls, value: str | None) -> str | None:
        return normalize_gstin(value)

    @field_validator("country")
    @classmethod
    def country_valid(cls, value: str | None) -> str | None:
        return validate_country(value) if value is not None else None

    @field_validator("gst_registration_status")
    @classmethod
    def status_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().casefold()
        if value not in _GST_STATUS:
            raise ValueError("Invalid GST registration status.")
        return value

    @model_validator(mode="after")
    def reject_null_for_nonnullable_fields(self):
        for field_name in ("business_name", "proprietor_name", "business_type", "address_line_1", "city", "state", "pin_code", "country"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null.")
        if "gstin" in self.model_fields_set or "gst_registration_status" in self.model_fields_set:
            _validate_status(self.gstin, self.gst_registration_status)
        return self


class BusinessCreateRequest(BusinessInputBase):
    business_name: str = Field(min_length=1, max_length=255)
    proprietor_name: str = Field(min_length=1, max_length=255)
    business_type: str = Field(min_length=1, max_length=64)
    address_line_1: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=128)
    pin_code: str = Field(min_length=1, max_length=16)
    country: str = "IN"
    gst_registration_status: str | None = None

    @model_validator(mode="after")
    def gst_status_consistent(self):
        self.gst_registration_status = _validate_status(self.gstin, self.gst_registration_status)
        if self.country == "IN" and not re.fullmatch(r"[1-9][0-9]{5}", self.pin_code):
            raise ValueError("Indian PIN code must contain six digits.")
        return self


class BusinessUpdateRequest(BusinessInputBase):
    @model_validator(mode="after")
    def has_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one business field must be provided.")
        if {"gstin", "gst_registration_status"}.issubset(self.model_fields_set):
            _validate_status(self.gstin, self.gst_registration_status)
        return self


class BusinessResponse(BaseModel):
    id: UUID
    business_name: str
    proprietor_name: str
    gstin: str | None
    business_type: str
    business_email: str | None
    business_phone: str | None
    address_line_1: str
    address_line_2: str | None
    city: str
    state: str
    pin_code: str
    country: str
    gst_registration_status: str
    gst_verification_note: str = "Self-reported; not verified against government records."
    created_at: datetime
    updated_at: datetime


class BusinessListResponse(BaseModel):
    businesses: list[BusinessResponse]


class BusinessMembershipResponse(BaseModel):
    membership_id: UUID
    user_id: UUID
    full_name: str
    email: str
    role: str
    created_at: datetime


class BusinessMembershipListResponse(BaseModel):
    business_id: UUID
    members: list[BusinessMembershipResponse]
