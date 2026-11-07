"""Request and response contracts for the shared business room."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["invoice", "bank_statement"]
    filename: str = Field(min_length=1, max_length=255)
    invoice_id: UUID | None = None


class ConversationMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    attachments: list[ConversationAttachment] = Field(default_factory=list, max_length=5)


class ConversationMessageResponse(BaseModel):
    id: UUID
    business_id: UUID
    sender_user_id: UUID | None
    sender_role: Literal["owner", "accountant", "agent"]
    sender_name: str
    content: str
    attachments: list[ConversationAttachment]
    created_at: datetime


class ConversationHistoryResponse(BaseModel):
    business_id: UUID
    messages: list[ConversationMessageResponse]
