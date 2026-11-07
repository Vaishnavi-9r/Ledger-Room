"""Contracts for external Mattermost identity and room mapping."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MattermostIdentityLinkResponse(BaseModel):
    authorization_url: str
    expires_at: datetime


class MattermostRoomResponse(BaseModel):
    id: UUID
    business_id: UUID
    mattermost_status: str
    mattermost_channel_id: str | None
    mattermost_channel_name: str | None
    mattermost_error_code: str | None
    created_at: datetime
    updated_at: datetime


class MattermostIdentityResponse(BaseModel):
    provider: str
    server_url: str
    mattermost_user_id: str

