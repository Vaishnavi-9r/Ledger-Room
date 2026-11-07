"""Accountant invitation and self-managed professional-profile routes."""

from __future__ import annotations

import logging
import hashlib
import re
from urllib.parse import urlencode
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.session import get_db
from ..dependencies.auth import AuthenticatedUser, get_current_auth, get_verified_auth
from ..models import BusinessInvitation, User
from ..schemas.accountant import (
    AccountantProfileResponse,
    AccountantProfileUpdateRequest,
    InvitationAcceptRequest,
    InvitationAcceptedResponse,
    InvitationCreateRequest,
    InvitationCreatedResponse,
    InvitationListResponse,
    InvitationResponse,
    InvitationValidationResponse,
)
from ..schemas.auth import normalize_email
from ..services.accountant_invitation_service import (
    InvitationError,
    accept_invitation,
    create_invitation,
    get_accountant_profile,
    list_invitations,
    owner_membership,
    revoke_invitation,
    save_accountant_profile,
    validate_invitation,
)
from ..services.auth_email import EmailProviderConfigurationError, development_invitation_link, get_email_provider
from ..services.auth_security import auth_rate_limiter

logger = logging.getLogger(__name__)
business_router = APIRouter(prefix="/api/v1/businesses", tags=["Accountant Invitations"])
accountant_router = APIRouter(tags=["Accountants"])


class _InvitationAccessLogFilter(logging.Filter):
    """Redact bearer invitation tokens from Uvicorn's request access line."""

    _token_query = re.compile(r"([?&](?:token|code|state)=)[^&\s\"]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = self._token_query.sub(r"\1[REDACTED]", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


logging.getLogger("uvicorn.access").addFilter(_InvitationAccessLogFilter())


def _rate_limit(request: Request, action: str, identifier: str, *, limit: int = 5) -> None:
    settings = get_settings()
    host = request.client.host if request.client else "unknown"
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    for key in (f"{action}:ip:{host}", f"{action}:identity:{digest}"):
        retry_after = auth_rate_limiter.check(
            key, limit=limit, window_seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
        )
        if retry_after:
            raise HTTPException(
                status_code=429, detail="Too many invitation attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )


def _invitation_response(invitation: BusinessInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id, business_id=invitation.business_id, email=invitation.email,
        role=invitation.role, status=invitation.status, expires_at=invitation.expires_at,
        created_at=invitation.created_at, accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
    )


def _require_owner(db: Session, business_id: UUID, user_id: UUID):
    result = owner_membership(db, business_id, user_id)
    if (
        result is None or result[0].is_demo
        or result[1].role != "owner" or result[1].status != "active"
    ):
        raise HTTPException(status_code=404, detail="Business not found.")
    return result


def _invitation_error(exc: InvitationError) -> HTTPException:
    if exc.code == "duplicate":
        return HTTPException(status_code=409, detail="An active invitation already exists for this email.")
    if exc.code == "email_mismatch":
        return HTTPException(status_code=403, detail="Sign in with the email address this invitation was sent to.")
    if exc.code == "unverified":
        return HTTPException(status_code=403, detail="A verified, active account is required to accept this invitation.")
    if exc.code == "membership_exists":
        return HTTPException(status_code=409, detail="An active membership already exists for this business.")
    if exc.code == "membership_conflict":
        return HTTPException(status_code=409, detail="This account already has a different membership for the business.")
    if exc.code == "expired":
        return HTTPException(status_code=410, detail="This invitation has expired.")
    if exc.code == "inactive":
        return HTTPException(status_code=410, detail="This invitation is no longer active.")
    return HTTPException(status_code=404, detail="Invitation not found or invalid.")


def _db_error(operation: str, exc: SQLAlchemyError) -> HTTPException:
    logger.error("Accountant operation failed operation=%s exception_type=%s", operation, type(exc).__name__)
    return HTTPException(status_code=503, detail="Accountant service is unavailable.")


def _shareable_invitation_link(token: str) -> str | None:
    """Return the one-time link to the authenticated owner who must share it."""
    base = (get_settings().AUTH_PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return None
    return f"{base}/accountant-invitations/accept?{urlencode({'token': token})}"


@business_router.post("/{business_id}/invitations", response_model=InvitationCreatedResponse, status_code=201)
def invite_accountant(
    business_id: UUID,
    payload: InvitationCreateRequest,
    request: Request,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> InvitationCreatedResponse:
    email_normalized = normalize_email(payload.email)
    _rate_limit(request, "accountant-invite", email_normalized)
    try:
        business, membership = _require_owner(db, business_id, principal.user.id)
        if email_normalized == principal.user.email_normalized:
            raise HTTPException(status_code=422, detail="Owners cannot invite their own account as an accountant.")
        try:
            provider = get_email_provider(get_settings())
        except EmailProviderConfigurationError as exc:
            logger.warning("Accountant invitation email unavailable exception_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Invitation email service is not configured.") from None
        settings = get_settings()
        dev_link_enabled = settings.APP_ENV.casefold() in {"development", "local", "test"} and settings.AUTH_DEV_RETURN_INVITATION_LINK
        if provider is None and not dev_link_enabled:
            raise HTTPException(status_code=503, detail="Invitation email service is not configured.")
        invitation, token = create_invitation(
            db, business=business, inviter_membership=membership,
            email=payload.email.strip(), email_normalized=email_normalized,
        )
        if provider is None:
            return InvitationCreatedResponse(
                invitation=_invitation_response(invitation),
                email_submission="development_link_only",
                development_invitation_link=development_invitation_link(token, settings),
                shareable_invitation_link=_shareable_invitation_link(token),
            )
        try:
            provider.send_accountant_invitation(invitation.email, token, business.business_name)
        except Exception as exc:
            # The raw token is never logged or returned. Revoke the unusable
            # pending record if the configured provider rejects submission.
            logger.warning("Accountant invitation email submission failed exception_type=%s", type(exc).__name__)
            db.rollback()
            invitation.status = "revoked"
            invitation.revoked_at = datetime.now(timezone.utc)
            db.commit()
            raise HTTPException(status_code=503, detail="Invitation email could not be submitted.") from None
        return InvitationCreatedResponse(
            invitation=_invitation_response(invitation),
            email_submission="submitted_to_configured_provider",
            shareable_invitation_link=_shareable_invitation_link(token),
        )
    except HTTPException:
        raise
    except InvitationError as exc:
        raise _invitation_error(exc) from None
    except IntegrityError as exc:
        db.rollback()
        if "pending_business_email" in str(getattr(exc, "orig", "")):
            raise HTTPException(status_code=409, detail="An active invitation already exists for this email.") from None
        raise _db_error("create_invitation", exc) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error("create_invitation", exc) from None


@business_router.get("/{business_id}/invitations", response_model=InvitationListResponse)
def get_invitations(
    business_id: UUID,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> InvitationListResponse:
    try:
        _require_owner(db, business_id, principal.user.id)
        rows = list_invitations(db, business_id)
        now = datetime.now(timezone.utc)
        for row in rows:
            if row.status == "pending" and row.expires_at <= now:
                row.status = "expired"  # response-only; no history is discarded
        return InvitationListResponse(invitations=[_invitation_response(row) for row in rows])
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise _db_error("list_invitations", exc) from None


@business_router.post("/{business_id}/invitations/{invitation_id}/revoke", response_model=InvitationResponse)
def revoke_accountant_invitation(
    business_id: UUID,
    invitation_id: UUID,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> InvitationResponse:
    try:
        _require_owner(db, business_id, principal.user.id)
        invitation = db.scalar(select(BusinessInvitation).where(
            BusinessInvitation.id == invitation_id,
            BusinessInvitation.business_id == business_id,
        ).with_for_update())
        if invitation is None:
            raise HTTPException(status_code=404, detail="Invitation not found.")
        return _invitation_response(revoke_invitation(db, invitation))
    except HTTPException:
        raise
    except InvitationError as exc:
        raise _invitation_error(exc) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error("revoke_invitation", exc) from None


@accountant_router.get("/api/v1/accountant-invitations/validate", response_model=InvitationValidationResponse)
def validate_accountant_invitation(
    request: Request,
    token: str = Query(min_length=32, max_length=256),
    db: Session = Depends(get_db),
) -> InvitationValidationResponse:
    _rate_limit(request, "accountant-invitation-validate", token, limit=10)
    try:
        invitation, business = validate_invitation(db, token)
        verification_note = (
            "verify this email before accepting" if get_settings().AUTH_REQUIRE_EMAIL_VERIFICATION
            else "sign in with this email before accepting"
        )
        return InvitationValidationResponse(
            invitation_id=invitation.id, business_name=business.business_name,
            invited_email=invitation.email, expires_at=invitation.expires_at,
            message=f"Invitation is active. Register or sign in and {verification_note}.",
        )
    except InvitationError as exc:
        raise _invitation_error(exc) from None
    except SQLAlchemyError as exc:
        raise _db_error("validate_invitation", exc) from None


@accountant_router.post("/api/v1/accountant-invitations/accept", response_model=InvitationAcceptedResponse)
def accept_accountant_invitation(
    payload: InvitationAcceptRequest,
    request: Request,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> InvitationAcceptedResponse:
    _rate_limit(request, "accountant-invitation-accept", payload.token, limit=10)
    try:
        invitation, membership = accept_invitation(db, raw_token=payload.token, user=principal.user)
        from ..models import BusinessRoom
        from ..services.mattermost_service import sync_business_room
        room = db.scalar(select(BusinessRoom).where(BusinessRoom.business_id == invitation.business_id))
        mattermost_status = sync_business_room(db, room).mattermost_status if room is not None else None
        return InvitationAcceptedResponse(
            invitation_id=invitation.id, business_id=invitation.business_id,
            membership_id=membership.id,
            mattermost_status=mattermost_status,
            message="Invitation accepted. Your accountant membership is active.",
        )
    except InvitationError as exc:
        raise _invitation_error(exc) from None
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Membership could not be created; verify that no membership already exists.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error("accept_invitation", exc) from None


def _profile_response(profile, user: User) -> AccountantProfileResponse:
    return AccountantProfileResponse(
        user_id=user.id, full_name=user.full_name, email=user.email, phone=profile.phone,
        firm_name=profile.firm_name, office_address=profile.office_address,
        professional_qualification=profile.professional_qualification,
        professional_registration_number=profile.professional_registration_number,
        icai_membership_number=profile.icai_membership_number,
        verification_status=profile.verification_status,
        created_at=profile.created_at, updated_at=profile.updated_at,
    )


@accountant_router.get("/api/v1/accountants/me", response_model=AccountantProfileResponse)
def get_my_accountant_profile(
    principal: AuthenticatedUser = Depends(get_current_auth), db: Session = Depends(get_db),
) -> AccountantProfileResponse:
    try:
        result = get_accountant_profile(db, principal.user.id)
        if result is None:
            raise HTTPException(status_code=404, detail="Accountant profile not found.")
        return _profile_response(*result)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise _db_error("get_profile", exc) from None


@accountant_router.put("/api/v1/accountants/me", response_model=AccountantProfileResponse)
def put_my_accountant_profile(
    payload: AccountantProfileUpdateRequest,
    principal: AuthenticatedUser = Depends(get_current_auth), db: Session = Depends(get_db),
) -> AccountantProfileResponse:
    try:
        profile = save_accountant_profile(db, principal.user, payload)
        return _profile_response(profile, principal.user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Accountant profile could not be saved.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error("save_profile", exc) from None

