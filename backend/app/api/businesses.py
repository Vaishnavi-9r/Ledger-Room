"""Authenticated business registration, profile, and member-list routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..dependencies.auth import AuthenticatedUser, get_current_auth, get_verified_auth
from ..models import Business
from ..schemas.business import (
    BusinessCreateRequest,
    BusinessListResponse,
    BusinessMembershipListResponse,
    BusinessMembershipResponse,
    BusinessResponse,
    BusinessUpdateRequest,
)
from ..services.business_service import (
    create_business,
    get_member_business,
    list_active_members,
    list_user_businesses,
    update_business,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/businesses", tags=["Businesses"])


def _response(row: Business) -> BusinessResponse:
    return BusinessResponse(
        id=row.id,
        business_name=row.business_name,
        proprietor_name=row.proprietor_name,
        gstin=row.gstin,
        business_type=row.business_type,
        business_email=row.business_email,
        business_phone=row.business_phone,
        address_line_1=row.address_line_1,
        address_line_2=row.address_line_2,
        city=row.city,
        state=row.state,
        pin_code=row.pin_code,
        country=row.country,
        gst_registration_status=row.gst_registration_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _database_error(operation: str, exc: SQLAlchemyError) -> HTTPException:
    logger.error("Business operation failed operation=%s exception_type=%s", operation, type(exc).__name__)
    return HTTPException(status_code=503, detail="Business service is unavailable.")


def _member_or_404(db: Session, business_id: UUID, user_id: UUID) -> tuple[Business, object]:
    result = get_member_business(db, business_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Business not found.")
    return result


@router.post("/", response_model=BusinessResponse, status_code=status.HTTP_201_CREATED)
def register_business(
    payload: BusinessCreateRequest,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> BusinessResponse:
    try:
        business = create_business(db, principal.user, payload)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Business could not be registered with the supplied details.") from None
    except SQLAlchemyError as exc:
        raise _database_error("register", exc) from None
    return _response(business)


@router.get("/", response_model=BusinessListResponse)
def list_businesses(
    principal: AuthenticatedUser = Depends(get_current_auth),
    db: Session = Depends(get_db),
) -> BusinessListResponse:
    try:
        rows = list_user_businesses(db, principal.user.id)
    except SQLAlchemyError as exc:
        raise _database_error("list", exc) from None
    return BusinessListResponse(businesses=[_response(row) for row in rows])


@router.get("/{business_id}", response_model=BusinessResponse)
def get_business(
    business_id: UUID,
    principal: AuthenticatedUser = Depends(get_current_auth),
    db: Session = Depends(get_db),
) -> BusinessResponse:
    try:
        business, _membership = _member_or_404(db, business_id, principal.user.id)
    except SQLAlchemyError as exc:
        raise _database_error("get", exc) from None
    return _response(business)


@router.patch("/{business_id}", response_model=BusinessResponse)
def patch_business(
    business_id: UUID,
    payload: BusinessUpdateRequest,
    principal: AuthenticatedUser = Depends(get_current_auth),
    db: Session = Depends(get_db),
) -> BusinessResponse:
    try:
        business, membership = _member_or_404(db, business_id, principal.user.id)
        if business.is_demo:
            raise HTTPException(status_code=404, detail="Business not found.")
        if membership.role != "owner":
            raise HTTPException(status_code=403, detail="Only a business owner can update this profile.")
        business = update_business(db, business, payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        raise HTTPException(status_code=409, detail="GSTIN is already associated with another business.") from None
    except SQLAlchemyError as exc:
        raise _database_error("update", exc) from None
    return _response(business)


@router.get("/{business_id}/members", response_model=BusinessMembershipListResponse)
def list_business_members(
    business_id: UUID,
    principal: AuthenticatedUser = Depends(get_current_auth),
    db: Session = Depends(get_db),
) -> BusinessMembershipListResponse:
    try:
        _business, _membership = _member_or_404(db, business_id, principal.user.id)
        rows = list_active_members(db, business_id)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise _database_error("members", exc) from None
    return BusinessMembershipListResponse(
        business_id=business_id,
        members=[
            BusinessMembershipResponse(
                membership_id=membership.id,
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                role=membership.role,
                created_at=membership.created_at,
            )
            for membership, user in rows
        ],
    )
