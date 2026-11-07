"""Email provider abstraction for verification messages."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlencode

from ..core.config import Settings, get_settings


class EmailProvider(Protocol):
    def send_verification(self, recipient: str, token: str) -> None: ...
    def send_accountant_invitation(self, recipient: str, token: str, business_name: str) -> None: ...


class EmailProviderConfigurationError(RuntimeError):
    pass


class SMTPEmailProvider:
    """SMTP/TLS delivery. Successful submission does not guarantee inbox delivery."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def verification_link(self, token: str) -> str:
        base = (self.settings.AUTH_PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            raise EmailProviderConfigurationError("AUTH_PUBLIC_BASE_URL is required for email delivery.")
        return f"{base}/verify-email?{urlencode({'token': token})}"

    def send_verification(self, recipient: str, token: str) -> None:
        password = self.settings.SMTP_PASSWORD.get_secret_value() if self.settings.SMTP_PASSWORD else None
        message = EmailMessage()
        message["Subject"] = "Verify your LedgerRoom email"
        message["From"] = self.settings.SMTP_FROM_EMAIL
        message["To"] = recipient
        message.set_content(
            "Verify your LedgerRoom email using the link below. The link expires "
            f"in {self.settings.AUTH_EMAIL_VERIFICATION_TTL_HOURS} hour(s).\n\n"
            f"{self.verification_link(token)}\n"
        )
        with smtplib.SMTP(self.settings.SMTP_HOST, self.settings.SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if self.settings.SMTP_USERNAME:
                smtp.login(self.settings.SMTP_USERNAME, password or "")
            smtp.send_message(message)

    def send_accountant_invitation(self, recipient: str, token: str, business_name: str) -> None:
        base = (self.settings.AUTH_PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            raise EmailProviderConfigurationError("AUTH_PUBLIC_BASE_URL is required for email delivery.")
        invitation_link = f"{base}/accountant-invitations/accept?{urlencode({'token': token})}"
        password = self.settings.SMTP_PASSWORD.get_secret_value() if self.settings.SMTP_PASSWORD else None
        message = EmailMessage()
        message["Subject"] = f"Invitation to join {business_name} on LedgerRoom"
        message["From"] = self.settings.SMTP_FROM_EMAIL
        message["To"] = recipient
        message.set_content(
            f"You have been invited to join {business_name} as an accountant in LedgerRoom.\n\n"
            "Sign in or create and verify your own LedgerRoom account, then accept this invitation:\n"
            f"{invitation_link}\n\nThis invitation expires in 7 days and can be used once."
        )
        with smtplib.SMTP(self.settings.SMTP_HOST, self.settings.SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if self.settings.SMTP_USERNAME:
                smtp.login(self.settings.SMTP_USERNAME, password or "")
            smtp.send_message(message)


def get_email_provider(settings: Settings | None = None) -> EmailProvider | None:
    settings = settings or get_settings()
    smtp_configuration = (settings.SMTP_HOST, settings.SMTP_FROM_EMAIL)
    auth_fields = (settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
    if not any(smtp_configuration) and not any(auth_fields):
        return None
    if not all(smtp_configuration) or not settings.AUTH_PUBLIC_BASE_URL:
        raise EmailProviderConfigurationError("SMTP_HOST, SMTP_FROM_EMAIL, and AUTH_PUBLIC_BASE_URL must be configured together.")
    if bool(settings.SMTP_USERNAME) != bool(settings.SMTP_PASSWORD):
        raise EmailProviderConfigurationError("SMTP_USERNAME and SMTP_PASSWORD must be configured together.")
    return SMTPEmailProvider(settings)


def development_verification_link(token: str, settings: Settings) -> str:
    if settings.APP_ENV.casefold() not in {"development", "local", "test"}:
        raise EmailProviderConfigurationError("Development verification links are disabled outside development.")
    base = (settings.AUTH_PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        raise EmailProviderConfigurationError("AUTH_PUBLIC_BASE_URL is required for a development verification link.")
    return f"{base}/verify-email?{urlencode({'token': token})}"


def development_invitation_link(token: str, settings: Settings) -> str:
    if settings.APP_ENV.casefold() not in {"development", "local", "test"} or not settings.AUTH_DEV_RETURN_INVITATION_LINK:
        raise EmailProviderConfigurationError("Development invitation links are disabled.")
    base = (settings.AUTH_PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        raise EmailProviderConfigurationError("AUTH_PUBLIC_BASE_URL is required for a development invitation link.")
    return f"{base}/accountant-invitations/accept?{urlencode({'token': token})}"
