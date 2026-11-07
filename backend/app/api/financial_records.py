"""Tenant-scoped expected vendor management and audit event access."""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context, require_owner
from ..models import ChaseEvent, ExpectedVendor
from ..schemas.financial_records import (
    ChaseEventResponse,
    ExpectedVendorInput,
    ExpectedVendorPatch,
    ExpectedVendorResponse,
)
from ..services.reconciliation_service import normalize_vendor_name

router = APIRouter(tags=["Financial Records"])
logger = logging.getLogger(__name__)


def _error(exc: SQLAlchemyError) -> HTTPException:
    logger.error("Tenant-scoped financial query failed exception_type=%s", type(exc).__name__)
    return HTTPException(status_code=503, detail="Financial records are unavailable.")


@router.get("/api/v1/expected-vendors/", response_model=list[ExpectedVendorResponse])
def list_expected_vendors(
    context: BusinessContext = Depends(get_business_context), db: Session = Depends(get_db),
):
    try:
        rows = db.scalars(select(ExpectedVendor).where(
            ExpectedVendor.business_id == context.business_id,
        ).order_by(ExpectedVendor.name, ExpectedVendor.id)).all()
        return rows
    except SQLAlchemyError as exc:
        raise _error(exc) from None


@router.post("/api/v1/expected-vendors/", response_model=ExpectedVendorResponse, status_code=status.HTTP_201_CREATED)
def create_expected_vendor(
    payload: ExpectedVendorInput,
    context: BusinessContext = Depends(get_business_context), db: Session = Depends(get_db),
):
    require_owner(context)
    normalized = normalize_vendor_name(payload.name)
    if not normalized:
        raise HTTPException(status_code=422, detail="Vendor name must contain searchable characters.")
    row = ExpectedVendor(
        id=uuid4(), business_id=context.business_id, name=payload.name,
        normalized_name=normalized, aliases=payload.aliases,
        currency=payload.currency.upper() if payload.currency else None,
        active=payload.active,
    )
    try:
        db.add(row)
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This vendor already exists for the selected business.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _error(exc) from None


@router.patch("/api/v1/expected-vendors/{vendor_id}", response_model=ExpectedVendorResponse)
def update_expected_vendor(
    vendor_id: str,
    payload: ExpectedVendorPatch,
    context: BusinessContext = Depends(get_business_context), db: Session = Depends(get_db),
):
    require_owner(context)
    from uuid import UUID
    try:
        parsed_id = UUID(vendor_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Expected vendor not found.") from None
    row = db.scalar(select(ExpectedVendor).where(
        ExpectedVendor.id == parsed_id,
        ExpectedVendor.business_id == context.business_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(status_code=404, detail="Expected vendor not found.")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        normalized = normalize_vendor_name(changes["name"])
        if not normalized:
            raise HTTPException(status_code=422, detail="Vendor name must contain searchable characters.")
        row.name = changes["name"].strip()
        row.normalized_name = normalized
    if "aliases" in changes:
        row.aliases = changes["aliases"] or []
    if "currency" in changes:
        row.currency = changes["currency"].upper() if changes["currency"] else None
    if "active" in changes:
        row.active = changes["active"]
    try:
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This vendor already exists for the selected business.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _error(exc) from None


@router.get("/api/v1/chase-events/", response_model=list[ChaseEventResponse])
def list_chase_events(
    context: BusinessContext = Depends(get_business_context), db: Session = Depends(get_db),
):
    try:
        rows = db.scalars(select(ChaseEvent).where(
            ChaseEvent.business_id == context.business_id,
        ).order_by(ChaseEvent.created_at.desc(), ChaseEvent.id)).all()
        return rows
    except SQLAlchemyError as exc:
        raise _error(exc) from None
