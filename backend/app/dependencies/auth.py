"""Bearer access-token dependency."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.session import get_db
from ..models import AuthToken, User
from ..services.auth_security import AuthConfigurationError, decode_access_token, token_claim_ids

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    family_id: UUID
    token_id: str


def get_current_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise unauthorized
    try:
        claims = decode_access_token(credentials.credentials)
        user_id, family_id = token_claim_ids(claims)
        user_query = select(User).where(
            User.id == user_id,
            User.is_active.is_(True),
        )
        if get_settings().AUTH_REQUIRE_EMAIL_VERIFICATION:
            user_query = user_query.where(User.email_verified_at.is_not(None))
        user = db.scalar(user_query)
        if user is None:
            raise unauthorized
        active_family_token = db.scalar(
            select(AuthToken.id).where(
                AuthToken.user_id == user_id,
                AuthToken.family_id == family_id,
                AuthToken.purpose == "refresh",
                AuthToken.consumed_at.is_(None),
                AuthToken.revoked_at.is_(None),
                AuthToken.expires_at > datetime.now(timezone.utc),
            ).limit(1)
        )
        if active_family_token is None:
            raise unauthorized
        return AuthenticatedUser(user=user, family_id=family_id, token_id=str(claims["jti"]))
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except HTTPException:
        raise
    except (InvalidTokenError, ValueError, TypeError, KeyError):
        raise unauthorized from None
    except SQLAlchemyError as exc:
        logger.error("Authentication lookup failed exception_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.") from None


def get_verified_auth(principal: AuthenticatedUser = Depends(get_current_auth)) -> AuthenticatedUser:
    """Require verified email before protected account setup operations."""
    if get_settings().AUTH_REQUIRE_EMAIL_VERIFICATION and principal.user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="Email verification is required.")
    return principal


def get_optional_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedUser | None:
    """Allow legacy demo reads without auth; validate any supplied bearer token."""
    if credentials is None:
        return None
    return get_current_auth(credentials, db)
