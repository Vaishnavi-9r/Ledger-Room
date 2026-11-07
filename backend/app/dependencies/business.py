"""Require an active membership in the explicitly selected business."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.auth import AuthenticatedUser, get_current_auth
from ..models import Business, BusinessMembership
from ..services.business_service import get_member_business


@dataclass(frozen=True)
class BusinessContext:
    business: Business
    membership: BusinessMembership
    principal: AuthenticatedUser

    @property
    def business_id(self) -> UUID:
        return self.business.id

    @property
    def role(self) -> str:
        return self.membership.role


def get_business_context(
    business_id: UUID = Query(...),
    principal: AuthenticatedUser = Depends(get_current_auth),
    db: Session = Depends(get_db),
) -> BusinessContext:
    """Resolve the tenant from a requested ID only after membership validation."""
    try:
        result = get_member_business(db, business_id, principal.user.id)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Business authorization is unavailable.") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Business not found.")
    business, membership = result
    if membership.status != "active":
        raise HTTPException(status_code=404, detail="Business not found.")
    # Auth and membership checks are read-only. Close their transaction here so
    # write services can open their own explicit atomic transaction on the
    # request-scoped Session.
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Business authorization is unavailable.") from exc
    return BusinessContext(business=business, membership=membership, principal=principal)


def require_owner(context: BusinessContext) -> None:
    """Reject accountants from operations that mutate owner-controlled data."""
    if context.role != "owner":
        raise HTTPException(status_code=403, detail="This operation requires an active business owner.")
