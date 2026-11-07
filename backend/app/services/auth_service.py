"""Database-backed account registration, verification, login, and refresh flows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..models import AuthToken, BusinessMembership, User
from .auth_security import (
    AuthConfigurationError,
    create_access_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)

INVALID_LOGIN = "Invalid email or password."
INVALID_TOKEN = "The token is invalid or expired."

# A one-time dummy credential check keeps unknown-email login attempts close to
# the cost of real password checks. It is never associated with an account.
_DUMMY_HASH = hash_password("LedgerRoom dummy password not used for login")


class DuplicateAccountError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_auth_token(
    *, user_id: UUID, purpose: str, token: str, expires_at: datetime,
    family_id: UUID | None = None,
) -> AuthToken:
    return AuthToken(
        id=uuid4(), user_id=user_id, purpose=purpose,
        token_hash=hash_opaque_token(token), family_id=family_id,
        expires_at=expires_at,
    )


def register_user(db: Session, *, full_name: str, email: str, normalized_email: str,
                  mobile_number: str, password: str, settings: Settings | None = None) -> tuple[User, str]:
    settings = settings or get_settings()
    if db.scalar(select(User.id).where(User.email_normalized == normalized_email).limit(1)) is not None:
        raise DuplicateAccountError
    now = _now()
    user = User(
        id=uuid4(), full_name=full_name, email=email, email_normalized=normalized_email,
        mobile_number=mobile_number, password_hash=hash_password(password),
        email_verified_at=None, is_active=True, created_at=now, updated_at=now,
    )
    raw_token = generate_opaque_token()
    token_row = _new_auth_token(
        user_id=user.id, purpose="email_verification", token=raw_token,
        expires_at=now + timedelta(hours=settings.AUTH_EMAIL_VERIFICATION_TTL_HOURS),
    )
    db.add(user)
    db.add(token_row)
    db.commit()
    return user, raw_token


def create_resend_token(db: Session, *, normalized_email: str,
                        settings: Settings | None = None) -> tuple[str, str] | None:
    settings = settings or get_settings()
    user = db.scalar(
        select(User).where(User.email_normalized == normalized_email).with_for_update()
    )
    if user is None or not user.is_active or user.email_verified_at is not None:
        return None
    now = _now()
    db.execute(
        update(AuthToken).where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == "email_verification",
            AuthToken.consumed_at.is_(None),
            AuthToken.revoked_at.is_(None),
        ).values(revoked_at=now)
    )
    raw_token = generate_opaque_token()
    db.add(_new_auth_token(
        user_id=user.id, purpose="email_verification", token=raw_token,
        expires_at=now + timedelta(hours=settings.AUTH_EMAIL_VERIFICATION_TTL_HOURS),
    ))
    db.commit()
    return user.email, raw_token


def verify_email_token(db: Session, *, raw_token: str) -> User:
    now = _now()
    token_ref = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_opaque_token(raw_token),
            AuthToken.purpose == "email_verification",
        )
    )
    if token_ref is None:
        raise InvalidCredentialsError(INVALID_TOKEN)
    # Use the same lock order as resend (user, then token) to avoid lock-order
    # deadlocks when verification and resend arrive concurrently.
    user = db.scalar(select(User).where(User.id == token_ref.user_id).with_for_update())
    if user is None or not user.is_active:
        raise InvalidCredentialsError(INVALID_TOKEN)
    token = db.scalar(select(AuthToken).where(AuthToken.id == token_ref.id).with_for_update())
    if token is None or token.consumed_at is not None or token.revoked_at is not None or token.expires_at <= now:
        raise InvalidCredentialsError(INVALID_TOKEN)
    if user.email_verified_at is None:
        user.email_verified_at = now
    token.consumed_at = now
    db.execute(
        update(AuthToken).where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == "email_verification",
            AuthToken.id != token.id,
            AuthToken.consumed_at.is_(None),
            AuthToken.revoked_at.is_(None),
        ).values(revoked_at=now)
    )
    db.commit()
    return user


def authenticate_user(db: Session, *, normalized_email: str, password: str) -> tuple[User, str, str, int, int]:
    settings = get_settings()
    user = db.scalar(select(User).where(User.email_normalized == normalized_email))
    password_ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if (
        not password_ok or user is None or not user.is_active
        or (settings.AUTH_REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None)
    ):
        raise InvalidCredentialsError(INVALID_LOGIN)

    now = _now()
    family_id = uuid4()
    access_token, access_lifetime = create_access_token(user_id=user.id, family_id=family_id, settings=settings, now=now)
    refresh_token = generate_opaque_token()
    refresh_expiry = now + timedelta(days=settings.AUTH_REFRESH_TOKEN_TTL_DAYS)
    db.add(_new_auth_token(
        user_id=user.id, purpose="refresh", token=refresh_token,
        expires_at=refresh_expiry, family_id=family_id,
    ))
    db.commit()
    return user, access_token, refresh_token, access_lifetime, settings.AUTH_REFRESH_TOKEN_TTL_DAYS


def rotate_refresh_token(db: Session, *, raw_token: str) -> tuple[User, str, str, int, int]:
    settings = get_settings()
    now = _now()
    token = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_opaque_token(raw_token),
            AuthToken.purpose == "refresh",
        ).with_for_update()
    )
    if token is None:
        raise InvalidCredentialsError(INVALID_TOKEN)
    if token.family_id is None:
        # Inconsistent legacy/foreign data must never mint a token.
        token.revoked_at = now
        db.commit()
        raise InvalidCredentialsError(INVALID_TOKEN)
    if token.consumed_at is not None or token.revoked_at is not None or token.expires_at <= now:
        db.execute(
            update(AuthToken).where(
                AuthToken.purpose == "refresh", AuthToken.family_id == token.family_id,
                AuthToken.revoked_at.is_(None),
            ).values(revoked_at=now)
        )
        db.commit()
        raise InvalidCredentialsError(INVALID_TOKEN)

    user = db.scalar(select(User).where(User.id == token.user_id).with_for_update())
    if (
        user is None or not user.is_active
        or (settings.AUTH_REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None)
    ):
        token.revoked_at = now
        db.commit()
        raise InvalidCredentialsError(INVALID_TOKEN)
    token.consumed_at = now
    access_token, access_lifetime = create_access_token(
        user_id=user.id, family_id=token.family_id, settings=settings, now=now,
    )
    replacement = generate_opaque_token()
    db.add(_new_auth_token(
        user_id=user.id, purpose="refresh", token=replacement,
        expires_at=now + timedelta(days=settings.AUTH_REFRESH_TOKEN_TTL_DAYS),
        family_id=token.family_id,
    ))
    db.commit()
    return user, access_token, replacement, access_lifetime, settings.AUTH_REFRESH_TOKEN_TTL_DAYS


def revoke_refresh_family(db: Session, *, family_id: UUID) -> None:
    db.execute(
        update(AuthToken).where(
            AuthToken.purpose == "refresh", AuthToken.family_id == family_id,
            AuthToken.revoked_at.is_(None),
        ).values(revoked_at=_now())
    )
    db.commit()


def active_memberships(db: Session, user_id: UUID) -> list[BusinessMembership]:
    return list(db.scalars(
        select(BusinessMembership).where(
            BusinessMembership.user_id == user_id,
            BusinessMembership.status == "active",
        ).order_by(BusinessMembership.created_at, BusinessMembership.id)
    ).all())
