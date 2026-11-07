"""Authenticated Mattermost linking and owner-managed private room mapping."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.session import get_db
from ..dependencies.auth import AuthenticatedUser, get_verified_auth
from ..models import Business, BusinessMembership, BusinessRoom, ExternalIdentity, MattermostOAuthState
from ..schemas.mattermost import MattermostIdentityResponse, MattermostRoomResponse
from ..services.auth_security import generate_opaque_token
from ..services.mattermost_service import (
    MattermostError,
    canonical_server_url,
    exchange_code,
    hash_state,
    oauth_configuration,
    pkce_pair,
    sync_business_room,
    sync_rooms_for_user,
)

router = APIRouter(tags=["Mattermost Integration"])


def _error(exc: MattermostError) -> HTTPException:
    if exc.code in {"oauth_not_configured", "mattermost_not_configured", "invalid_configuration"}:
        return HTTPException(status_code=503, detail=exc.public_message)
    if exc.code in {"mattermost_forbidden", "identity_verification_failed", "oauth_exchange_failed"}:
        return HTTPException(status_code=403, detail=exc.public_message)
    if exc.code == "mattermost_unavailable":
        return HTTPException(status_code=503, detail=exc.public_message)
    return HTTPException(status_code=502, detail=exc.public_message)


def _room_response(room: BusinessRoom) -> MattermostRoomResponse:
    return MattermostRoomResponse(
        id=room.id,
        business_id=room.business_id,
        mattermost_status=room.mattermost_status,
        mattermost_channel_id=room.mattermost_channel_id,
        mattermost_channel_name=room.mattermost_channel_name,
        mattermost_error_code=room.mattermost_error_code,
        created_at=room.created_at,
        updated_at=room.updated_at,
    )


@router.get("/api/v1/integrations/mattermost/me", response_model=MattermostIdentityResponse)
def get_mattermost_identity(
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> MattermostIdentityResponse:
    try:
        server = canonical_server_url()
        identity = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.user_id == principal.user.id,
            ExternalIdentity.provider == "mattermost",
            ExternalIdentity.workspace_id == server,
        ))
        if identity is None:
            raise HTTPException(status_code=404, detail="Link your Mattermost account first.")
        return MattermostIdentityResponse(
            provider=identity.provider,
            server_url=server,
            mattermost_user_id=identity.external_user_id,
        )
    except MattermostError as exc:
        raise _error(exc) from None


@router.post("/api/v1/integrations/mattermost/link")
def start_mattermost_identity_link(
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> dict[str, str | datetime]:
    settings = get_settings()
    try:
        server, client_id, _client_secret = oauth_configuration(settings)
        state = generate_opaque_token()
        verifier, challenge = pkce_pair()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.MATTERMOST_OAUTH_STATE_TTL_SECONDS)
        db.add(MattermostOAuthState(
            id=uuid4(), state_hash=hash_state(state), user_id=principal.user.id,
            code_verifier=verifier, expires_at=expires_at,
        ))
        db.commit()
        callback = settings.MATTERMOST_OAUTH_CALLBACK_URL.strip()
        authorization_url = f"{server}/oauth/authorize?" + urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": callback,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return {"authorization_url": authorization_url, "expires_at": expires_at}
    except MattermostError as exc:
        raise _error(exc) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Mattermost identity linking is unavailable.") from exc


@router.get("/api/v1/integrations/mattermost/callback", response_model=None)
def finish_mattermost_identity_link(
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    state: str = Query(min_length=32, max_length=256),
    error: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> dict[str, str] | RedirectResponse:
    """Consume a one-time state bound to the LedgerRoom user who began OAuth."""
    if error or not code:
        raise HTTPException(status_code=400, detail="Mattermost account linking was not completed.")
    now = datetime.now(timezone.utc)
    try:
        row = db.scalar(select(MattermostOAuthState).where(
            MattermostOAuthState.state_hash == hash_state(state),
        ).with_for_update())
        if row is None or row.consumed_at is not None or row.expires_at <= now:
            raise HTTPException(status_code=400, detail="Mattermost identity link state is invalid or expired.")
        row.consumed_at = now
        user_id = row.user_id
        verifier = row.code_verifier
        db.commit()

        server, verified_identity = exchange_code(code=code, verifier=verifier)
        external_id = verified_identity["id"]
        owner_of_external_id = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.provider == "mattermost",
            ExternalIdentity.workspace_id == server,
            ExternalIdentity.external_user_id == external_id,
        ))
        if owner_of_external_id is not None and owner_of_external_id.user_id != user_id:
            raise HTTPException(status_code=409, detail="This Mattermost account is already linked to another LedgerRoom account.")
        existing_for_user = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.provider == "mattermost",
            ExternalIdentity.workspace_id == server,
            ExternalIdentity.user_id == user_id,
        ))
        if existing_for_user is not None and existing_for_user.external_user_id != external_id:
            raise HTTPException(status_code=409, detail="A different Mattermost account is already linked. Contact support to change it.")
        if existing_for_user is None:
            db.add(ExternalIdentity(
                id=uuid4(), user_id=user_id, provider="mattermost",
                workspace_id=server, external_user_id=external_id,
            ))
            db.commit()
        rooms = sync_rooms_for_user(db, user_id)
        status = "linked"
        if any(room.mattermost_status in {"pending_configuration", "pending_identity_link", "partial", "error"} for room in rooms):
            status = "linked_room_sync_pending"
        frontend_base = (get_settings().AUTH_PUBLIC_BASE_URL or "").rstrip("/")
        if frontend_base:
            return RedirectResponse(
                url=f"{frontend_base}/?mattermost=linked",
                status_code=303,
            )
        return {"status": status, "message": "Mattermost identity verified and linked to your LedgerRoom account."}
    except HTTPException:
        raise
    except MattermostError as exc:
        db.rollback()
        raise _error(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This Mattermost identity is already linked.") from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Mattermost identity linking is unavailable.") from exc


@router.post("/api/v1/businesses/{business_id}/room", response_model=MattermostRoomResponse)
def create_or_sync_business_room(
    business_id: str,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> MattermostRoomResponse:
    """Create the canonical finance room and provision its mapped private channel."""
    from uuid import UUID
    try:
        parsed_id = UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Business not found.") from None
    try:
        membership = db.scalar(select(BusinessMembership).where(
            BusinessMembership.business_id == parsed_id,
            BusinessMembership.user_id == principal.user.id,
            BusinessMembership.status == "active",
            BusinessMembership.role == "owner",
        ))
        business = db.get(Business, parsed_id)
        if membership is None or business is None or business.is_demo:
            raise HTTPException(status_code=404, detail="Business not found.")
        room = db.scalar(select(BusinessRoom).where(BusinessRoom.business_id == parsed_id).with_for_update())
        if room is None:
            room = BusinessRoom(id=uuid4(), business_id=parsed_id, mattermost_status="pending")
            db.add(room)
            db.commit()
            db.refresh(room)
        room = sync_business_room(db, room)
        return _room_response(room)
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        room = db.scalar(select(BusinessRoom).where(BusinessRoom.business_id == parsed_id))
        if room is None:
            raise HTTPException(status_code=503, detail="Finance room could not be created.") from None
        return _room_response(sync_business_room(db, room))
    except MattermostError as exc:
        db.rollback()
        raise _error(exc) from None
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Finance room could not be created.") from exc


@router.get("/api/v1/businesses/{business_id}/room", response_model=MattermostRoomResponse)
def get_business_room(
    business_id: str,
    principal: AuthenticatedUser = Depends(get_verified_auth),
    db: Session = Depends(get_db),
) -> MattermostRoomResponse:
    from uuid import UUID
    try:
        parsed_id = UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Business room not found.") from None
    member = db.scalar(select(BusinessMembership.id).where(
        BusinessMembership.business_id == parsed_id,
        BusinessMembership.user_id == principal.user.id,
        BusinessMembership.status == "active",
    ))
    room = db.scalar(select(BusinessRoom).where(BusinessRoom.business_id == parsed_id))
    if member is None or room is None:
        raise HTTPException(status_code=404, detail="Business room not found.")
    return _room_response(room)
