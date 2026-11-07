"""Mattermost OAuth identity linking and private finance-channel provisioning."""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..models import Business, BusinessMembership, BusinessRoom, ExternalIdentity


class MattermostError(RuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def canonical_server_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    value = (settings.MATTERMOST_BASE_URL or "").strip().rstrip("/")
    parsed = urlsplit(value)
    allow_local_http = settings.APP_ENV.casefold() in {"development", "local", "test"}
    if (
        parsed.scheme not in ({"https", "http"} if allow_local_http else {"https"})
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise MattermostError("invalid_configuration", "Mattermost server URL is not configured correctly.")
    if len(value) > 128:
        raise MattermostError("invalid_configuration", "Mattermost server URL must be 128 characters or fewer.")
    return value


def oauth_configuration(settings: Settings | None = None) -> tuple[str, str, str]:
    settings = settings or get_settings()
    server = canonical_server_url(settings)
    client_id = (settings.MATTERMOST_OAUTH_CLIENT_ID or "").strip()
    client_secret = settings.MATTERMOST_OAUTH_CLIENT_SECRET.get_secret_value().strip() if settings.MATTERMOST_OAUTH_CLIENT_SECRET else ""
    callback = (settings.MATTERMOST_OAUTH_CALLBACK_URL or "").strip()
    parsed_callback = urlsplit(callback)
    if not client_id or not client_secret or not callback or parsed_callback.scheme != "https" and settings.APP_ENV.casefold() not in {"development", "local", "test"}:
        raise MattermostError("oauth_not_configured", "Mattermost identity linking is not configured.")
    if parsed_callback.scheme not in {"https", "http"} or not parsed_callback.hostname:
        raise MattermostError("invalid_configuration", "Mattermost OAuth callback URL is invalid.")
    return server, client_id, client_secret


def service_configuration(settings: Settings | None = None) -> tuple[str, str, str]:
    settings = settings or get_settings()
    server = canonical_server_url(settings)
    team_id = (settings.MATTERMOST_TEAM_ID or "").strip()
    bot_token = settings.MATTERMOST_BOT_TOKEN.get_secret_value().strip() if settings.MATTERMOST_BOT_TOKEN else ""
    if not team_id or not bot_token:
        raise MattermostError("mattermost_not_configured", "Mattermost channel provisioning is not configured.")
    return server, team_id, bot_token


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(__import__("secrets").token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _request(method: str, server: str, path: str, *, token: str, payload: dict | None = None) -> dict:
    try:
        response = httpx.request(
            method,
            f"{server}/api/v4/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=10,
        )
    except httpx.RequestError as exc:
        raise MattermostError("mattermost_unavailable", "Mattermost could not be reached.") from exc
    if response.status_code >= 400:
        if response.status_code in {401, 403}:
            code, message = "mattermost_forbidden", "Mattermost rejected the configured service account or its permissions."
        elif response.status_code == 404:
            code, message = "mattermost_not_found", "Mattermost could not find the configured team or channel resource."
        else:
            code, message = "mattermost_request_failed", "Mattermost rejected a channel operation."
        raise MattermostError(code, message)
    try:
        data = response.json()
    except ValueError as exc:
        raise MattermostError("mattermost_invalid_response", "Mattermost returned an invalid response.") from exc
    if not isinstance(data, dict):
        raise MattermostError("mattermost_invalid_response", "Mattermost returned an invalid response.")
    return data


def exchange_code(*, code: str, verifier: str, settings: Settings | None = None) -> tuple[str, dict]:
    settings = settings or get_settings()
    server, client_id, client_secret = oauth_configuration(settings)
    callback = (settings.MATTERMOST_OAUTH_CALLBACK_URL or "").strip()
    try:
        response = httpx.post(
            f"{server}/oauth/access_token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
                "code": code,
                "code_verifier": verifier,
            },
            timeout=10,
        )
    except httpx.RequestError as exc:
        raise MattermostError("mattermost_unavailable", "Mattermost could not be reached.") from exc
    if response.status_code >= 400:
        raise MattermostError("oauth_exchange_failed", "Mattermost identity verification failed.")
    try:
        payload = response.json()
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
    except ValueError as exc:
        raise MattermostError("oauth_exchange_failed", "Mattermost identity verification failed.") from exc
    if not isinstance(access_token, str) or not access_token:
        raise MattermostError("oauth_exchange_failed", "Mattermost identity verification failed.")
    try:
        identity_response = httpx.get(
            f"{server}/api/v4/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except httpx.RequestError as exc:
        raise MattermostError("mattermost_unavailable", "Mattermost could not be reached.") from exc
    if identity_response.status_code >= 400:
        raise MattermostError("identity_verification_failed", "Mattermost could not verify the signed-in account.")
    try:
        identity = identity_response.json()
    except ValueError as exc:
        raise MattermostError("identity_verification_failed", "Mattermost could not verify the signed-in account.") from exc
    if not isinstance(identity, dict) or not isinstance(identity.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", identity["id"]):
        raise MattermostError("identity_verification_failed", "Mattermost could not verify the signed-in account.")
    return server, identity


def _ensure_channel_member(server: str, token: str, channel_id: str, user_id: str) -> None:
    try:
        response = httpx.get(
            f"{server}/api/v4/channels/{channel_id}/members/{user_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except httpx.RequestError as exc:
        raise MattermostError("mattermost_unavailable", "Mattermost could not be reached.") from exc
    if response.status_code == 200:
        return
    if response.status_code != 404:
        if response.status_code in {401, 403}:
            raise MattermostError("mattermost_forbidden", "Mattermost service account cannot manage channel members.")
        raise MattermostError("mattermost_request_failed", "Mattermost could not check channel membership.")
    _request("POST", server, f"channels/{channel_id}/members", token=token, payload={"user_id": user_id})


def sync_business_room(db: Session, room: BusinessRoom, *, settings: Settings | None = None) -> BusinessRoom:
    """Create the private channel once, then idempotently provision linked members."""
    settings = settings or get_settings()
    business = db.get(Business, room.business_id)
    if business is None:
        raise MattermostError("business_not_found", "Business room was not found.")
    try:
        server, team_id, service_token = service_configuration(settings)
    except MattermostError as exc:
        room.mattermost_status = "pending_configuration"
        room.mattermost_error_code = exc.code
        db.commit()
        return room

    if room.mattermost_channel_id and (
        room.mattermost_server != server or room.mattermost_team_id != team_id
    ):
        room.mattermost_status = "error"
        room.mattermost_error_code = "server_mapping_conflict"
        db.commit()
        return room

    memberships = db.scalars(select(BusinessMembership).where(
        BusinessMembership.business_id == room.business_id,
        BusinessMembership.status == "active",
    )).all()
    external_ids: list[str] = []
    missing_identity = False
    for membership in memberships:
        identity = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.user_id == membership.user_id,
            ExternalIdentity.provider == "mattermost",
            ExternalIdentity.workspace_id == server,
        ))
        if identity is None:
            missing_identity = True
        else:
            external_ids.append(identity.external_user_id)

    # Create only after the owner has explicitly linked their own Mattermost account.
    owner_membership = next((m for m in memberships if m.role == "owner"), None)
    owner_identity = db.scalar(select(ExternalIdentity).where(
        ExternalIdentity.user_id == owner_membership.user_id if owner_membership else False,
        ExternalIdentity.provider == "mattermost",
        ExternalIdentity.workspace_id == server,
    )) if owner_membership else None
    if owner_identity is None:
        room.mattermost_status = "pending_identity_link"
        room.mattermost_error_code = "owner_identity_not_linked"
        room.mattermost_server = server
        room.mattermost_team_id = team_id
        db.commit()
        return room

    if not room.mattermost_channel_id:
        name = f"lr-{room.id.hex[:12]}"
        display_name = f"{business.business_name} Finance"[:64]
        try:
            created = _request("POST", server, "channels", token=service_token, payload={
                "team_id": team_id,
                "name": name,
                "display_name": display_name,
                "type": "P",
                "purpose": f"LedgerRoom finance room {room.id}",
            })
        except MattermostError as exc:
            room.mattermost_status = "error"
            room.mattermost_error_code = exc.code
            room.mattermost_server = server
            room.mattermost_team_id = team_id
            db.commit()
            return room
        channel_id = created.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            raise MattermostError("mattermost_invalid_response", "Mattermost did not confirm the channel creation.")
        room.mattermost_server = server
        room.mattermost_team_id = team_id
        room.mattermost_channel_id = channel_id
        room.mattermost_channel_name = name
        db.commit()
        db.refresh(room)

    try:
        for external_id in sorted(set(external_ids)):
            _ensure_channel_member(server, service_token, room.mattermost_channel_id, external_id)
        switch_user_id = (settings.MATTERMOST_SWITCH_BOT_USER_ID or "").strip()
        if switch_user_id:
            _ensure_channel_member(server, service_token, room.mattermost_channel_id, switch_user_id)
        room.mattermost_status = "ready" if not missing_identity and switch_user_id else "partial"
        room.mattermost_error_code = (
            "member_identity_not_linked" if missing_identity else
            "switch_agent_identity_not_configured" if not switch_user_id else None
        )
    except MattermostError as exc:
        room.mattermost_status = "error"
        room.mattermost_error_code = exc.code
    db.commit()
    db.refresh(room)
    return room


def sync_rooms_for_user(db: Session, user_id: UUID, *, settings: Settings | None = None) -> list[BusinessRoom]:
    room_ids = db.scalars(
        select(BusinessRoom).join(BusinessMembership, BusinessMembership.business_id == BusinessRoom.business_id)
        .where(BusinessMembership.user_id == user_id, BusinessMembership.status == "active")
    ).all()
    return [sync_business_room(db, room, settings=settings) for room in room_ids]
