"""Account authentication and email verification endpoints."""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..db.session import get_db
from ..dependencies.auth import AuthenticatedUser, get_current_auth
from ..models import AuthToken
from ..schemas.auth import (
    BusinessMembershipSummary,
    CurrentUserResponse,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegistrationResponse,
    RegisterRequest,
    ResendVerificationRequest,
    TokenResponse,
    VerifyEmailRequest,
    VerificationDeliveryResponse,
    normalize_email,
)
from ..services.auth_email import (
    EmailProviderConfigurationError,
    development_verification_link,
    get_email_provider,
)
from ..services.auth_security import (
    AuthConfigurationError,
    auth_rate_limiter,
    hash_opaque_token,
)
from ..services.auth_service import (
    DuplicateAccountError,
    INVALID_LOGIN,
    INVALID_TOKEN,
    InvalidCredentialsError,
    active_memberships,
    authenticate_user,
    create_resend_token,
    register_user,
    revoke_refresh_family,
    rotate_refresh_token,
    verify_email_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _rate_limit(request: Request, action: str, identifier: str | None = None,
                *, limit: int | None = None) -> None:
    settings = get_settings()
    configured_limit = limit or settings.AUTH_RATE_LIMIT_MAX_REQUESTS
    host = request.client.host if request.client else "unknown"
    keys = [f"{action}:ip:{host}"]
    if identifier:
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        keys.append(f"{action}:identity:{digest}")
    for key in keys:
        retry_after = auth_rate_limiter.check(
            key, limit=configured_limit,
            window_seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
        )
        if retry_after:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many authentication attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )


def _loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        import ipaddress
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _deliver_verification(email: str, token: str, settings: Settings, *, allow_dev_link: bool = False) -> tuple[str | None, str]:
    """Send via configured SMTP or expose a link only in opted-in local development."""
    delivery_status = "unavailable"
    try:
        provider = get_email_provider(settings)
        if provider is not None:
            provider.send_verification(email, token)
            return None, "submitted"
    except EmailProviderConfigurationError as exc:
        logger.warning("Verification email configuration unavailable exception_type=%s", type(exc).__name__)
        delivery_status = "failed"
    except Exception as exc:
        logger.warning("Verification message submission failed exception_type=%s", type(exc).__name__)
        delivery_status = "failed"

    development_environment = settings.APP_ENV.casefold() in {"development", "local", "test"}
    if development_environment and settings.AUTH_DEV_RETURN_VERIFICATION_LINK and allow_dev_link:
        try:
            return development_verification_link(token, settings), "development_link"
        except EmailProviderConfigurationError as exc:
            logger.warning("Development verification link unavailable exception_type=%s", type(exc).__name__)
            delivery_status = "unavailable"
    return None, delivery_status


def _safe_db_failure(operation: str, exc: SQLAlchemyError) -> HTTPException:
    logger.error("Authentication operation failed operation=%s exception_type=%s", operation, type(exc).__name__)
    return HTTPException(status_code=503, detail="Authentication service is unavailable.")


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_202_ACCEPTED)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> RegistrationResponse:
    normalized = normalize_email(payload.email)
    _rate_limit(request, "register", normalized)
    settings = get_settings()
    try:
        user, raw_token = register_user(
            db, full_name=payload.full_name, email=payload.email.strip(),
            normalized_email=normalized, mobile_number=payload.mobile_number,
            password=payload.password, settings=settings,
        )
    except DuplicateAccountError:
        raise HTTPException(status_code=409, detail="An account with this email is already registered.") from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email is already registered.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("register", exc) from None
    except Exception:
        db.rollback()
        logger.error("Authentication operation failed operation=register exception_type=internal")
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.") from None

    if settings.AUTH_REQUIRE_EMAIL_VERIFICATION:
        dev_link, delivery_status = _deliver_verification(
            user.email, raw_token, settings, allow_dev_link=_loopback_request(request),
        )
    else:
        dev_link, delivery_status = None, "not_required"
    messages = {
        "submitted": "Registration received. Verification email submitted to the configured provider.",
        "development_link": "Registration received. Use the local development link to verify your email.",
        "failed": "Your account was created, but the verification email could not be submitted. Check email configuration and request a new link.",
        "unavailable": "Your account was created, but email delivery is unavailable. Configure SMTP, or set AUTH_PUBLIC_BASE_URL and enable loopback-only development links, then request a new link.",
        "not_required": "Registration complete. Email verification is disabled in this development environment; you can sign in now.",
    }
    return RegistrationResponse(
        message=messages[delivery_status],
        email_verification_required=settings.AUTH_REQUIRE_EMAIL_VERIFICATION,
        email_delivery_status=delivery_status,
        development_verification_link=dev_link,
    )


@router.post("/verify-email", response_model=MessageResponse)
def verify_email(payload: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)) -> MessageResponse:
    _rate_limit(request, "verify-email", payload.token, limit=10)
    try:
        verify_email_token(db, raw_token=payload.token)
    except InvalidCredentialsError:
        raise HTTPException(status_code=400, detail=INVALID_TOKEN) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("verify-email", exc) from None
    return MessageResponse(message="Email verified. You can now sign in.")


@router.post("/resend-verification", response_model=VerificationDeliveryResponse, status_code=status.HTTP_202_ACCEPTED)
def resend_verification(payload: ResendVerificationRequest, request: Request, db: Session = Depends(get_db)) -> VerificationDeliveryResponse:
    normalized = normalize_email(payload.email)
    _rate_limit(request, "resend-verification", normalized, limit=3)
    settings = get_settings()
    try:
        result = create_resend_token(db, normalized_email=normalized, settings=settings)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("resend-verification", exc) from None
    dev_link = None
    if result:
        email, token = result
        dev_link, _delivery_status = _deliver_verification(email, token, settings, allow_dev_link=_loopback_request(request))
    return VerificationDeliveryResponse(
        message="If the account is eligible, verification instructions will be sent when delivery is configured.",
        development_verification_link=dev_link,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    normalized = normalize_email(payload.email)
    _rate_limit(request, "login", normalized)
    try:
        _, access, refresh, expires_in, _refresh_days = authenticate_user(
            db, normalized_email=normalized, password=payload.password,
        )
    except InvalidCredentialsError:
        raise HTTPException(status_code=401, detail=INVALID_LOGIN, headers={"WWW-Authenticate": "Bearer"}) from None
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("login", exc) from None
    return TokenResponse(
        access_token=access, refresh_token=refresh, expires_in=expires_in,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    token_hash = hash_opaque_token(payload.refresh_token)
    _rate_limit(request, "refresh", token_hash)
    try:
        _, access, replacement, expires_in, _refresh_days = rotate_refresh_token(db, raw_token=payload.refresh_token)
    except InvalidCredentialsError:
        raise HTTPException(status_code=401, detail=INVALID_TOKEN) from None
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("refresh", exc) from None
    return TokenResponse(
        access_token=access, refresh_token=replacement, expires_in=expires_in,
    )


@router.post("/logout", response_model=MessageResponse)
def logout(principal: AuthenticatedUser = Depends(get_current_auth), db: Session = Depends(get_db)) -> MessageResponse:
    try:
        revoke_refresh_family(db, family_id=principal.family_id)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _safe_db_failure("logout", exc) from None
    return MessageResponse(message="Signed out.")


@router.get("/me", response_model=CurrentUserResponse)
def current_user(principal: AuthenticatedUser = Depends(get_current_auth), db: Session = Depends(get_db)) -> CurrentUserResponse:
    try:
        memberships = active_memberships(db, principal.user.id)
    except SQLAlchemyError as exc:
        raise _safe_db_failure("me", exc) from None
    user = principal.user
    return CurrentUserResponse(
        id=user.id, full_name=user.full_name, email=user.email,
        mobile_number=user.mobile_number, email_verified=user.email_verified_at is not None,
        memberships=[BusinessMembershipSummary(business_id=row.business_id, role=row.role) for row in memberships],
    )
