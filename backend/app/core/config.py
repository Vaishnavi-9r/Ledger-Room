"""Environment-backed application settings."""

from functools import lru_cache
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIRECTORY = Path(__file__).resolve().parents[2]
# A local launcher may explicitly select an isolated config file. The default
# remains backend/.env, so existing production deployments are unaffected.
_configured_env_file = os.environ.get("LEDGERROOM_CONFIG_FILE", "").strip()
CONFIG_ENV_FILE = (
    Path(_configured_env_file).expanduser()
    if _configured_env_file
    else BACKEND_DIRECTORY / ".env"
)


class Settings(BaseSettings):
    """Application settings loaded from process environment or backend/.env."""

    model_config = SettingsConfigDict(
        env_file=CONFIG_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    GEMINI_API_KEY: SecretStr | None = None
    GEMINI_MODEL: str = "gemini-3.6-flash"
    RECEIPT_VERIFICATION_DATE_WINDOW_DAYS: int = Field(default=7, ge=0, le=3650)
    APP_ENV: str = "production"
    AUTH_JWT_SIGNING_KEY: SecretStr | None = None
    AUTH_ACCESS_TOKEN_TTL_SECONDS: int = Field(default=900, ge=60, le=3600)
    AUTH_REFRESH_TOKEN_TTL_DAYS: int = Field(default=30, ge=1, le=180)
    AUTH_EMAIL_VERIFICATION_TTL_HOURS: int = Field(default=24, ge=1, le=168)
    AUTH_REQUIRE_EMAIL_VERIFICATION: bool = True
    AUTH_PUBLIC_BASE_URL: str | None = None
    AUTH_DEV_RETURN_VERIFICATION_LINK: bool = False
    AUTH_DEV_RETURN_INVITATION_LINK: bool = False
    AUTH_RATE_LIMIT_MAX_REQUESTS: int = Field(default=5, ge=1, le=100)
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)
    SMTP_HOST: str | None = None
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_FROM_EMAIL: str | None = None
    EVIDENCE_STORAGE_BACKEND: Literal["local", "s3"] = "local"
    EVIDENCE_LOCAL_ROOT: Path | None = None
    EVIDENCE_DEFAULT_BUSINESS_QUOTA_BYTES: int = Field(default=1_073_741_824, ge=1)
    EVIDENCE_ORPHAN_RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    EVIDENCE_S3_ENDPOINT_URL: str | None = None
    EVIDENCE_S3_REGION: str | None = None
    EVIDENCE_S3_BUCKET: str | None = None
    EVIDENCE_S3_ACCESS_KEY_ID: SecretStr | None = None
    EVIDENCE_S3_SECRET_ACCESS_KEY: SecretStr | None = None
    EVIDENCE_S3_PRIVATE_BUCKET_CONFIRMED: bool = False
    MATTERMOST_BASE_URL: str | None = None
    MATTERMOST_TEAM_ID: str | None = None
    MATTERMOST_BOT_TOKEN: SecretStr | None = None
    MATTERMOST_SWITCH_BOT_USER_ID: str | None = None
    MATTERMOST_OAUTH_CLIENT_ID: str | None = None
    MATTERMOST_OAUTH_CLIENT_SECRET: SecretStr | None = None
    MATTERMOST_OAUTH_CALLBACK_URL: str | None = None
    MATTERMOST_OAUTH_STATE_TTL_SECONDS: int = Field(default=600, ge=60, le=1800)
    MATTERMOST_RELAY_ENABLED: bool = False

    @model_validator(mode="after")
    def email_verification_can_only_be_disabled_locally(self) -> "Settings":
        if not self.AUTH_REQUIRE_EMAIL_VERIFICATION and self.APP_ENV.casefold() not in {"development", "local", "test"}:
            raise ValueError("AUTH_REQUIRE_EMAIL_VERIFICATION may be disabled only in development, local, or test environments.")
        if self.AUTH_DEV_RETURN_INVITATION_LINK and self.APP_ENV.casefold() not in {"development", "local", "test"}:
            raise ValueError("AUTH_DEV_RETURN_INVITATION_LINK may be enabled only in development, local, or test environments.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance."""
    return Settings()


class DatabaseSettings(BaseSettings):
    """Database credentials loaded from process environment or backend/.env."""

    model_config = SettingsConfigDict(
        env_file=CONFIG_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: SecretStr | None = None


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """Return database settings from the same file used for app settings."""
    return DatabaseSettings()
