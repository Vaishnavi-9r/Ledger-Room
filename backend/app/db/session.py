"""SQLAlchemy engine and request-scoped session dependency."""

from functools import lru_cache
from typing import Generator

from fastapi import HTTPException, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ..core.config import get_database_settings, get_settings


class DatabaseConfigurationError(RuntimeError):
    """Raised when DATABASE_URL is missing or invalid."""


@lru_cache
def get_database_url() -> URL:
    """Load and normalize the root DATABASE_URL without exposing its contents."""
    configured_url = get_database_settings().DATABASE_URL
    if configured_url is None or not configured_url.get_secret_value().strip():
        raise DatabaseConfigurationError(
            "Required environment variable DATABASE_URL is missing "
            "(expected in backend/.env or the process environment)."
        )

    try:
        url = make_url(configured_url.get_secret_value())
    except ArgumentError:
        raise DatabaseConfigurationError("DATABASE_URL is invalid.") from None

    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise DatabaseConfigurationError("DATABASE_URL must use PostgreSQL.")

    query = dict(url.query)
    sslmode = query.get("sslmode")
    if isinstance(sslmode, tuple):
        sslmode = sslmode[-1] if sslmode else None
    settings = get_settings()
    local_host = (url.host or "").casefold() in {"localhost", "127.0.0.1", "::1"}
    local_development = settings.APP_ENV.casefold() in {"development", "local"}
    if local_development and (
        not local_host
        or url.port != 55432
        or url.database != "ledgerroom_mattermost_dev"
    ):
        raise DatabaseConfigurationError(
            "Local development must use the isolated loopback LedgerRoom Mattermost database."
        )
    local_development_database = local_development and local_host
    if local_development_database and sslmode is None:
        # The isolated loopback development container does not enable TLS.
        # This exception cannot affect remote hosts or production settings.
        pass
    elif sslmode not in {"require", "verify-ca", "verify-full"}:
        query["sslmode"] = "require"

    return url.set(drivername="postgresql+psycopg", query=query)


@lru_cache
def get_engine() -> Engine:
    """Create a reusable engine with Supabase-compatible SSL and pooling."""
    url = get_database_url()
    connect_args: dict[str, object] = {"connect_timeout": 10}
    common_options: dict[str, object] = {
        "connect_args": connect_args,
        "hide_parameters": True,
    }

    # Supabase's transaction pooler uses port 6543. Avoid a second client-side
    # pool and disable Psycopg automatic prepared statements in that mode.
    if url.port == 6543:
        connect_args["prepare_threshold"] = None
        return create_engine(url, poolclass=NullPool, **common_options)

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_recycle=1800,
        **common_options,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the shared SQLAlchemy session factory."""
    return sessionmaker(
        bind=get_engine(),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped session, returning a safe 503 if unconfigured."""
    try:
        factory = get_session_factory()
    except DatabaseConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None

    with factory() as session:
        yield session
