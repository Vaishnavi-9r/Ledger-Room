"""Response schemas for versioned monthly report approvals."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ApprovalStatus = Literal["draft", "pending_review", "changes_requested", "approved", "stale"]
ApprovalDecision = Literal["approved", "changes_requested"]


class ApprovalReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comments: str | None = Field(default=None, max_length=5000)


class ApprovalChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comments: str = Field(min_length=1, max_length=5000)


class MonthlyReportApprovalEventResponse(BaseModel):
    event_id: UUID
    actor_user_id: UUID
    actor_name: str
    event_type: str
    previous_status: ApprovalStatus | None
    resulting_status: ApprovalStatus
    comments: str | None
    report_fingerprint: str
    created_at: datetime


class MonthlyReportApprovalResponse(BaseModel):
    approval_id: UUID
    business_id: UUID
    report_year: int
    report_month: int
    version: int
    status: ApprovalStatus
    submitted_by: UUID
    submitted_by_name: str
    submitted_at: datetime
    reviewed_by: UUID | None
    reviewed_by_name: str | None
    reviewed_at: datetime | None
    accountant_comments: str | None
    approval_decision: ApprovalDecision | None
    report_fingerprint: str
    events: list[MonthlyReportApprovalEventResponse] = Field(default_factory=list)


class MonthlyReportApprovalListResponse(BaseModel):
    approvals: list[MonthlyReportApprovalResponse]


class MonthlyReportApprovalStatusResponse(BaseModel):
    approval: MonthlyReportApprovalResponse | None
