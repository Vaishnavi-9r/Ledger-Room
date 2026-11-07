"""Single-use business invitations and owner-maintained membership access."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import AccountantProfile, Business, BusinessInvitation, BusinessMembership, User
from ..services.auth_security import generate_opaque_token, hash_opaque_token

INVITATION_TTL = timedelta(days=7)


class InvitationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def owner_membership(db: Session, business_id: UUID, user_id: UUID) -> tuple[Business, BusinessMembership] | None:
    return db.execute(
        select(Business, BusinessMembership)
        .join(BusinessMembership, BusinessMembership.business_id == Business.id)
        .where(
            Business.id == business_id,
            BusinessMembership.user_id == user_id,
            BusinessMembership.role == "owner",
            BusinessMembership.status == "active",
        )
    ).first()


def list_invitations(db: Session, business_id: UUID) -> list[BusinessInvitation]:
    return list(db.scalars(
        select(BusinessInvitation)
        .where(BusinessInvitation.business_id == business_id)
        .order_by(BusinessInvitation.created_at.desc(), BusinessInvitation.id)
    ).all())


def create_invitation(
    db: Session, *, business: Business, inviter_membership: BusinessMembership,
    email: str, email_normalized: str,
) -> tuple[BusinessInvitation, str]:
    now = datetime.now(timezone.utc)
    try:
        # Expired pending rows are retained as history but no longer block a
        # fresh invitation. The partial unique index closes concurrent races.
        expired = db.scalars(select(BusinessInvitation).where(
            BusinessInvitation.business_id == business.id,
            BusinessInvitation.email_normalized == email_normalized,
            BusinessInvitation.status == "pending",
            BusinessInvitation.expires_at <= now,
        ).with_for_update()).all()
        for row in expired:
            row.status = "expired"

        active = db.scalar(select(BusinessInvitation.id).where(
            BusinessInvitation.business_id == business.id,
            BusinessInvitation.email_normalized == email_normalized,
            BusinessInvitation.status == "pending",
            BusinessInvitation.expires_at > now,
        ).limit(1))
        if active is not None:
            raise InvitationError("duplicate")

        raw_token = generate_opaque_token()
        invitation = BusinessInvitation(
            id=uuid4(), business_id=business.id,
            invited_by_membership_id=inviter_membership.id,
            email=email, email_normalized=email_normalized,
            role="accountant", token_hash=hash_opaque_token(raw_token),
            status="pending", expires_at=now + INVITATION_TTL,
            created_at=now,
        )
        db.add(invitation)
        db.commit()
        return invitation, raw_token
    except InvitationError:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise


def invitation_for_token(db: Session, raw_token: str, *, lock: bool = False) -> tuple[BusinessInvitation, Business] | None:
    token_hash = hash_opaque_token(raw_token)
    query = (
        select(BusinessInvitation, Business)
        .join(Business, Business.id == BusinessInvitation.business_id)
        .where(BusinessInvitation.token_hash == token_hash)
    )
    if lock:
        query = query.with_for_update(of=BusinessInvitation)
    return db.execute(query).first()


def validate_invitation(db: Session, raw_token: str) -> tuple[BusinessInvitation, Business]:
    result = invitation_for_token(db, raw_token)
    if result is None:
        raise InvitationError("invalid")
    invitation, business = result
    now = datetime.now(timezone.utc)
    if invitation.status == "pending" and invitation.expires_at <= now:
        raise InvitationError("expired")
    if invitation.status != "pending":
        raise InvitationError("inactive")
    return invitation, business


def revoke_invitation(db: Session, invitation: BusinessInvitation) -> BusinessInvitation:
    if invitation.status != "pending":
        raise InvitationError("inactive")
    now = datetime.now(timezone.utc)
    if invitation.expires_at <= now:
        invitation.status = "expired"
        db.commit()
        raise InvitationError("expired")
    invitation.status = "revoked"
    invitation.revoked_at = now
    db.commit()
    return invitation


def accept_invitation(db: Session, *, raw_token: str, user: User) -> tuple[BusinessInvitation, BusinessMembership]:
    result = invitation_for_token(db, raw_token, lock=True)
    if result is None:
        raise InvitationError("invalid")
    invitation, business = result
    now = datetime.now(timezone.utc)
    if invitation.status == "pending" and invitation.expires_at <= now:
        invitation.status = "expired"
        db.commit()
        raise InvitationError("expired")
    if invitation.status != "pending":
        raise InvitationError("inactive")
    if not hmac.compare_digest(invitation.email_normalized, user.email_normalized):
        raise InvitationError("email_mismatch")
    if not user.is_active or (get_settings().AUTH_REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None):
        raise InvitationError("unverified")

    try:
        existing = db.scalar(select(BusinessMembership).where(
            BusinessMembership.business_id == business.id,
            BusinessMembership.user_id == user.id,
        ).with_for_update())
        if existing is not None and existing.status == "active":
            raise InvitationError("membership_exists")
        if existing is not None and existing.role != "accountant":
            raise InvitationError("membership_conflict")

        if existing is None:
            membership = BusinessMembership(
                id=uuid4(), business_id=business.id, user_id=user.id,
                role="accountant", status="active", created_at=now,
            )
            db.add(membership)
        else:
            # Reuse the unique historical membership row; no duplicate can be
            # created, and a new invitation remains the acceptance evidence.
            existing.status = "active"
            existing.revoked_at = None
            membership = existing

        invitation.status = "accepted"
        invitation.accepted_at = now
        db.commit()
        return invitation, membership
    except InvitationError:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise


def get_accountant_profile(db: Session, user_id: UUID) -> tuple[AccountantProfile, User] | None:
    row = db.execute(
        select(AccountantProfile, User)
        .join(User, User.id == AccountantProfile.user_id)
        .where(AccountantProfile.user_id == user_id)
    ).first()
    return row if row is not None else None


def save_accountant_profile(db: Session, user: User, payload) -> AccountantProfile:
    now = datetime.now(timezone.utc)
    changes = payload.model_dump(exclude={"full_name"})
    try:
        if payload.full_name is not None:
            user.full_name = payload.full_name
            user.updated_at = now
        profile = db.scalar(select(AccountantProfile).where(
            AccountantProfile.user_id == user.id
        ).with_for_update())
        if profile is None:
            profile = AccountantProfile(
                user_id=user.id, **changes, verification_status="unverified",
                created_at=now, updated_at=now,
            )
            db.add(profile)
        else:
            for field, value in changes.items():
                setattr(profile, field, value)
            profile.updated_at = now
        db.commit()
        return profile
    except SQLAlchemyError:
        db.rollback()
        raise
