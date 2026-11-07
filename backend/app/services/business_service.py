"""Business profile operations with membership-based access control."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import Business, BusinessMembership, User
from ..schemas.business import BusinessCreateRequest, BusinessUpdateRequest, _validate_status


def list_user_businesses(db: Session, user_id: UUID) -> list[Business]:
    return list(db.scalars(
        select(Business)
        .join(BusinessMembership, BusinessMembership.business_id == Business.id)
        .where(
            BusinessMembership.user_id == user_id,
            BusinessMembership.status == "active",
        )
        .order_by(Business.created_at, Business.id)
    ).all())


def get_member_business(db: Session, business_id: UUID, user_id: UUID) -> tuple[Business, BusinessMembership] | None:
    row = db.execute(
        select(Business, BusinessMembership)
        .join(BusinessMembership, BusinessMembership.business_id == Business.id)
        .where(
            Business.id == business_id,
            BusinessMembership.user_id == user_id,
            BusinessMembership.status == "active",
        )
    ).first()
    return row if row is not None else None


def create_business(db: Session, owner: User, payload: BusinessCreateRequest) -> Business:
    data = payload.model_dump()
    now = datetime.now(timezone.utc)
    business = Business(id=uuid4(), **data, is_demo=False, created_at=now, updated_at=now)
    membership = BusinessMembership(
        id=uuid4(), business_id=business.id, user_id=owner.id,
        role="owner", status="active", created_at=now,
    )
    # A single commit makes business and owner membership atomic. A failure in
    # either INSERT is rolled back by the caller/session transaction.
    try:
        db.add(business)
        db.add(membership)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    return business


def update_business(db: Session, business: Business, payload: BusinessUpdateRequest) -> Business:
    changes = payload.model_dump(exclude_unset=True)
    effective_gstin = changes.get("gstin", business.gstin)
    effective_status = changes.get("gst_registration_status", business.gst_registration_status)
    if "gstin" in changes or "gst_registration_status" in changes:
        if (
            "gstin" in changes
            and "gst_registration_status" not in changes
            and effective_gstin is not None
            and effective_status in {"unregistered", "not_applicable"}
        ):
            effective_status = "registered"
        if effective_gstin is None and effective_status == "registered":
            # Removing a registered GSTIN should not leave a contradictory status.
            if "gst_registration_status" not in changes:
                effective_status = "unregistered"
            else:
                raise ValueError("GSTIN is required when GST registration status is registered.")
        effective_status = _validate_status(effective_gstin, effective_status)
        changes["gst_registration_status"] = effective_status
    country = changes.get("country", business.country)
    pin_code = changes.get("pin_code", business.pin_code)
    if country == "IN" and not re.fullmatch(r"[1-9][0-9]{5}", pin_code):
        raise ValueError("Indian PIN code must contain six digits.")
    for field, value in changes.items():
        setattr(business, field, value)
    business.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    return business


def list_active_members(db: Session, business_id: UUID) -> list[tuple[BusinessMembership, User]]:
    return list(db.execute(
        select(BusinessMembership, User)
        .join(User, User.id == BusinessMembership.user_id)
        .where(BusinessMembership.business_id == business_id, BusinessMembership.status == "active")
        .order_by(BusinessMembership.created_at, BusinessMembership.id)
    ).all())
