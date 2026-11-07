"""Password, JWT, refresh-token, and process-local throttling primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from jwt import InvalidTokenError

from ..core.config import Settings, get_settings

JWT_ISSUER = "ledgerroom"
JWT_AUDIENCE = "ledgerroom-api"
JWT_ALGORITHM = "HS256"
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


class AuthConfigurationError(RuntimeError):
    """Raised when authentication cannot run with the configured secrets."""


def jwt_signing_key(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    secret = settings.AUTH_JWT_SIGNING_KEY
    key = secret.get_secret_value() if secret else ""
    if len(key.encode("utf-8")) < 32:
        raise AuthConfigurationError("AUTH_JWT_SIGNING_KEY must be configured with at least 32 bytes.")
    return key


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
        p=SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024,
    )
    return "$".join((
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
            p=SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    *, user_id: UUID, family_id: UUID, settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    settings = settings or get_settings()
    issued = now or datetime.now(timezone.utc)
    expires_in = settings.AUTH_ACCESS_TOKEN_TTL_SECONDS
    claims = {
        "sub": str(user_id),
        "sid": str(family_id),
        "jti": secrets.token_urlsafe(18),
        "iat": issued,
        "nbf": issued,
        "exp": issued + timedelta(seconds=expires_in),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    return jwt.encode(claims, jwt_signing_key(settings), algorithm=JWT_ALGORITHM), expires_in


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return jwt.decode(
        token,
        jwt_signing_key(settings),
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        options={"require": ["sub", "sid", "jti", "iat", "nbf", "exp", "iss", "aud"]},
        leeway=0,
    )


def token_claim_ids(claims: dict[str, Any]) -> tuple[UUID, UUID]:
    user_id, family_id = claims.get("sub"), claims.get("sid")
    if not isinstance(user_id, str) or not isinstance(family_id, str):
        raise InvalidTokenError("Invalid token claims")
    return UUID(user_id), UUID(family_id)


class ProcessRateLimiter:
    """Thread-safe per-process sliding-window limiter for auth endpoints.

    Deployments with multiple workers should additionally rate-limit at their
    shared ingress. This in-process control is still enforced per worker.
    """

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> int:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, int(window_seconds - (now - events[0])))
            events.append(now)
            if len(self._events) > 10000:
                stale = [name for name, values in self._events.items() if not values or values[-1] <= cutoff]
                for name in stale:
                    self._events.pop(name, None)
                if len(self._events) > 10000:
                    excess = len(self._events) - 8000
                    for name in list(self._events)[:excess]:
                        self._events.pop(name, None)
            return 0

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


auth_rate_limiter = ProcessRateLimiter()
