"""Authenticated Mattermost WebSocket event relay for deterministic LedgerRoom commands.

Run as a separate worker with ``python -m backend.app.services.mattermost_event_relay``.
The authenticated Mattermost event is the only source of sender, channel, post ID,
and body. MCP tool arguments are not an authorization input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import websockets
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..db.session import get_session_factory
from ..models import (
    Business,
    BusinessMembership,
    BusinessRoom,
    ExternalIdentity,
    MattermostRelayEvent,
)
from .mattermost_service import MattermostError, _request, canonical_server_url, service_configuration

logger = logging.getLogger("ledgerroom.mattermost_relay")

_REPORT = re.compile(r"^/report(?:\s+(\d{4})-(\d{2}))?$", re.IGNORECASE)
_RESPOND = re.compile(r"^/respond\s+([0-9a-f-]{36})\s+(.+)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class MattermostPostEvent:
    post_id: str
    channel_id: str
    user_id: str
    message: str


@dataclass(frozen=True)
class RelayOutcome:
    status: str
    result_code: str
    channel_id: str | None = None
    reply: str | None = None
    post_id: str | None = None


def parse_posted_event(event: Any) -> MattermostPostEvent | None:
    """Decode Mattermost's documented ``posted`` frame and post payload."""
    if not isinstance(event, dict) or event.get("event") != "posted":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    raw_post = data.get("post")
    if isinstance(raw_post, str):
        try:
            post = json.loads(raw_post)
        except (TypeError, ValueError):
            return None
    else:
        post = raw_post
    if not isinstance(post, dict):
        return None
    post_id, channel_id, user_id, message = (
        post.get("id"), post.get("channel_id"), post.get("user_id"), post.get("message"),
    )
    if not all(isinstance(value, str) and value.strip() for value in (post_id, channel_id, user_id)):
        return None
    if not isinstance(message, str):
        return None
    if max(len(post_id), len(channel_id), len(user_id)) > 128:
        return None
    broadcast = event.get("broadcast")
    # Mattermost's broadcast channel, when present, must agree with the post.
    # broadcast.user_id describes the recipient of that broadcast, not the sender.
    if isinstance(broadcast, dict) and broadcast.get("channel_id") and broadcast["channel_id"] != channel_id:
        return None
    return MattermostPostEvent(post_id, channel_id, user_id, message)


def _command_kind(message: str) -> tuple[str, tuple[str, ...]] | None:
    text = message.strip()
    # Mattermost treats text beginning with "/" as a native slash command and
    # submits it to /api/v4/commands/execute. LedgerRoom intentionally does not
    # register unauthenticated command callbacks; use an ordinary post prefixed
    # with "lr" so it arrives via the authenticated posted-event WebSocket.
    if text.casefold().startswith("lr "):
        text = "/" + text[3:].lstrip()
    elif text.casefold() == "lr":
        return None
    if text.casefold() == "/reconcile":
        return "reconcile", ()
    if text.casefold() == "/clarifications":
        return "clarifications", ()
    report = _REPORT.fullmatch(text)
    if report:
        return "report", (report.group(1) or "", report.group(2) or "")
    response = _RESPOND.fullmatch(text)
    if response:
        try:
            request_id = str(UUID(response.group(1)))
        except ValueError:
            return None
        answer = response.group(2).strip()
        if answer:
            return "respond", (request_id, answer)
    return None


def _check_current_membership(
    db: Session, *, business_id: UUID, user_id: UUID, required_role: str | None,
) -> BusinessMembership:
    membership = db.scalar(select(BusinessMembership).where(
        BusinessMembership.business_id == business_id,
        BusinessMembership.user_id == user_id,
        BusinessMembership.status == "active",
    ).with_for_update())
    if membership is None or (required_role is not None and membership.role != required_role):
        raise PermissionError("membership_or_role_denied")
    return membership


def _reconcile(db: Session, business_id: UUID, user_id: UUID) -> str:
    from .reconciliation_service import run_reconciliation

    def authorize(session: Session) -> None:
        _check_current_membership(
            session, business_id=business_id, user_id=user_id, required_role="owner",
        )

    result = run_reconciliation(db, business_id, authorization_check=authorize)
    return (
        f"Deterministic reconciliation finished for {result.total_invoices} invoices: "
        f"{result.matched} matched, {result.amount_mismatch} amount mismatches, "
        f"{result.missing_payment} missing payments, {result.possible_duplicate} possible duplicates, "
        f"and {result.needs_review} needing review. Review source records before acting on any result."
    )


def _report(db: Session, business: Business, user_id: UUID, year: int, month: int) -> str:
    from .monthly_report_service import get_monthly_report

    if not 1 <= month <= 12 or not 1900 <= year <= 9998:
        return "Use `lr report YYYY-MM`, for example `lr report 2026-10`."
    with db.begin():
        _check_current_membership(
            db, business_id=business.id, user_id=user_id, required_role=None,
        )
        report = get_monthly_report(db, year, month, business.id, business)
        summary = report.summary
        return (
            f"Monthly report for {year}-{month:02d}: {summary.total_invoices_received} invoices, "
            f"{summary.total_bank_transactions} bank transactions, {summary.matched_payments} matched payments, "
            f"{summary.amount_mismatches} amount mismatches, {summary.missing_payments} missing payments, "
            f"{summary.possible_duplicates} possible duplicates, {summary.transactions_requiring_review} items needing review, "
            f"and {summary.pending_clarifications} pending clarifications."
        )


def _clarifications(db: Session, business_id: UUID, user_id: UUID) -> str:
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload
    from ..models import ClarificationRequest

    with db.begin():
        _check_current_membership(
            db, business_id=business_id, user_id=user_id, required_role=None,
        )
        rows = db.scalars(select(ClarificationRequest).where(
            ClarificationRequest.business_id == business_id,
            ClarificationRequest.status == "pending",
        ).options(selectinload(ClarificationRequest.bank_line)).order_by(
            ClarificationRequest.created_at.desc()).limit(5)).all()
        count = db.scalar(select(func.count()).select_from(ClarificationRequest).where(
            ClarificationRequest.business_id == business_id,
            ClarificationRequest.status == "pending",
        )) or 0
        if count == 0:
            return "There are no pending clarification requests for this business."
        details = " ".join(
            f"{index}. {row.question} (transaction: {row.bank_line.description}; request {row.id})."
            for index, row in enumerate(rows, start=1)
        )
        return f"There are {count} pending clarification(s). Up to five: {details}"


def _respond(db: Session, business_id: UUID, user_id: UUID, request_id: UUID, answer: str) -> str:
    from .clarification_service import respond_to_clarification

    def authorize(session: Session) -> None:
        _check_current_membership(
            session, business_id=business_id, user_id=user_id, required_role="owner",
        )

    request = respond_to_clarification(
        db, business_id, request_id, answer, authorization_check=authorize,
    )
    return f"Your response was saved to clarification {request.id}. Its current status is {request.status}."


def _execute_command(
    db: Session, *, command: tuple[str, tuple[str, ...]],
    room: BusinessRoom, user_id: UUID, role: str, business: Business,
) -> tuple[str, str]:
    kind, args = command
    if kind == "reconcile":
        if role != "owner":
            raise PermissionError("owner_required")
        return "completed", _reconcile(db, room.business_id, user_id)
    if kind == "report":
        year = int(args[0]) if args[0] else datetime.now(timezone.utc).year
        month = int(args[1]) if args[1] else datetime.now(timezone.utc).month
        return "completed", _report(db, business, user_id, year, month)
    if kind == "clarifications":
        return "completed", _clarifications(db, room.business_id, user_id)
    if kind == "respond":
        if role != "owner":
            raise PermissionError("owner_required")
        return "completed", _respond(db, room.business_id, user_id, UUID(args[0]), args[1])
    return "ignored", ""


def _finish(db: Session, row: MattermostRelayEvent, status: str, result_code: str) -> None:
    row.status = status
    row.result_code = result_code
    row.completed_at = datetime.now(timezone.utc)
    db.commit()


def process_authenticated_post(
    db: Session,
    event: Any,
    *,
    transport_authenticated: bool,
    mattermost_server: str,
    ignored_user_ids: frozenset[str] = frozenset(),
) -> RelayOutcome:
    """Authorize and execute one post received on an authenticated WS session."""
    if not transport_authenticated:
        return RelayOutcome("rejected", "unauthenticated_transport")
    post = parse_posted_event(event)
    if post is None:
        return RelayOutcome("rejected", "invalid_post_event")
    if post.user_id in ignored_user_ids:
        return RelayOutcome("ignored", "bot_self_message")
    command = _command_kind(post.message)
    try:
        row = MattermostRelayEvent(
            mattermost_server=mattermost_server,
            mattermost_post_id=post.post_id,
            mattermost_channel_id=post.channel_id,
            mattermost_user_id=post.user_id,
            status="processing",
        )
        db.add(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RelayOutcome("duplicate", "event_already_seen")
    except SQLAlchemyError:
        db.rollback()
        return RelayOutcome("failed", "event_reservation_failed")

    try:
        room = db.scalar(select(BusinessRoom).where(
            BusinessRoom.mattermost_server == mattermost_server,
            BusinessRoom.mattermost_channel_id == post.channel_id,
            BusinessRoom.mattermost_status.in_(("ready", "partial")),
        ))
        if room is None:
            _finish(db, row, "rejected", "unknown_channel")
            return RelayOutcome("rejected", "unknown_channel")
        row.business_room_id = room.id
        identity = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.provider == "mattermost",
            ExternalIdentity.workspace_id == mattermost_server,
            ExternalIdentity.external_user_id == post.user_id,
        ))
        if identity is None:
            _finish(db, row, "rejected", "unlinked_user")
            return RelayOutcome("rejected", "unlinked_user")
        row.user_id = identity.user_id
        membership = db.scalar(select(BusinessMembership).where(
            BusinessMembership.business_id == room.business_id,
            BusinessMembership.user_id == identity.user_id,
            BusinessMembership.status == "active",
        ))
        if membership is None:
            _finish(db, row, "rejected", "inactive_or_wrong_room_membership")
            return RelayOutcome("rejected", "inactive_or_wrong_room_membership")
        if command is None:
            _finish(db, row, "ignored", "not_a_ledgerroom_command")
            return RelayOutcome("ignored", "not_a_ledgerroom_command")
        business = db.get(Business, room.business_id)
        if business is None or business.is_demo:
            _finish(db, row, "rejected", "business_unavailable")
            return RelayOutcome("rejected", "business_unavailable")
        # Persist the trusted sender/room binding before dispatch; operations
        # re-check active membership inside their own transaction as well.
        db.commit()
        status, reply = _execute_command(
            db, command=command, room=room, user_id=identity.user_id,
            role=membership.role, business=business,
        )
        if status == "ignored":
            _finish(db, row, status, "unsupported_command")
            return RelayOutcome(status, "unsupported_command")
        return RelayOutcome("completed", "operation_completed", post.channel_id, reply, post.post_id)
    except PermissionError:
        db.rollback()
        _finish(db, row, "rejected", "role_forbidden")
        return RelayOutcome("rejected", "role_forbidden", post.channel_id, "This LedgerRoom action is not permitted for your business role.", post.post_id)
    except Exception:
        db.rollback()
        _finish(db, row, "failed", "operation_failed")
        return RelayOutcome("failed", "operation_failed", post.channel_id, "LedgerRoom could not complete that request. No result has been assumed.", post.post_id)


def post_agent_reply(*, channel_id: str, message: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    server, _team_id, token = service_configuration(settings)
    posted = _request("POST", server, "posts", token=token, payload={
        "channel_id": channel_id,
        "message": message[:16000],
    })
    post_id = posted.get("id")
    if not isinstance(post_id, str) or not post_id:
        raise MattermostError("mattermost_invalid_response", "Mattermost did not confirm the LedgerRoom reply.")
    return post_id


def deliver_outcome(
    db: Session, outcome: RelayOutcome, *, post_reply: Callable[[str, str], str] | None = None,
    mattermost_server: str,
) -> RelayOutcome:
    """Post responses to the originating channel and record delivery outcome."""
    if outcome.status == "duplicate" or outcome.channel_id is None or outcome.reply is None or outcome.post_id is None:
        return outcome
    try:
        if post_reply is None:
            post_reply = lambda channel_id, message: post_agent_reply(channel_id=channel_id, message=message)
        post_reply(outcome.channel_id, outcome.reply)
        row = db.scalar(select(MattermostRelayEvent).where(
            MattermostRelayEvent.mattermost_server == mattermost_server,
            MattermostRelayEvent.mattermost_post_id == outcome.post_id,
        ))
        if row is not None:
            final_status = "failed" if outcome.status == "failed" else "completed"
            result_code = "operation_failed_reply_delivered" if outcome.status == "failed" else "reply_delivered"
            _finish(db, row, final_status, result_code)
        return outcome
    except Exception:
        db.rollback()
        row = db.scalar(select(MattermostRelayEvent).where(
            MattermostRelayEvent.mattermost_server == mattermost_server,
            MattermostRelayEvent.mattermost_post_id == outcome.post_id,
        ))
        if row is not None:
            _finish(db, row, "failed", "reply_delivery_failed")
        return RelayOutcome("failed", "reply_delivery_failed", outcome.channel_id)


def websocket_url(server: str) -> str:
    parsed = urlsplit(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/api/v4/websocket", "", ""))


async def run_authenticated_connection(
    *,
    server: str,
    token: str,
    session_factory: Callable[[], Session],
    settings: Settings | None = None,
    ignored_user_ids: frozenset[str] = frozenset(),
) -> None:
    """Consume Mattermost events only after the token challenge is accepted."""
    sequence = 1
    authenticated = False
    async with websockets.connect(websocket_url(server), ping_interval=20, open_timeout=10) as socket:
        await socket.send(json.dumps({
            "seq": sequence,
            "action": "authentication_challenge",
            "data": {"token": token},
        }))
        while True:
            frame = json.loads(await socket.recv())
            if not isinstance(frame, dict):
                continue
            if frame.get("seq_reply") == sequence:
                if frame.get("status") != "OK":
                    raise RuntimeError("Mattermost WebSocket authentication failed")
                authenticated = True
                continue
            if not authenticated or frame.get("event") != "posted":
                continue
            outcome = await asyncio.to_thread(
                _process_and_reply,
                session_factory,
                frame,
                authenticated,
                server,
                settings,
                ignored_user_ids,
            )
            if outcome.status not in {"ignored", "duplicate"}:
                logger.info("Mattermost relay event result=%s code=%s", outcome.status, outcome.result_code)


def _process_and_reply(
    session_factory: Callable[[], Session],
    frame: dict[str, Any],
    authenticated: bool,
    server: str,
    settings: Settings | None,
    ignored_user_ids: frozenset[str] = frozenset(),
) -> RelayOutcome:
    """Keep each SQLAlchemy session on the single worker thread using it."""
    with session_factory() as db:
        try:
            outcome = process_authenticated_post(
                db, frame, transport_authenticated=authenticated,
                mattermost_server=server, ignored_user_ids=ignored_user_ids,
            )
            if outcome.reply and outcome.channel_id:
                outcome = deliver_outcome(
                    db,
                    outcome,
                    post_reply=lambda channel_id, message: post_agent_reply(
                        channel_id=channel_id, message=message, settings=settings,
                    ),
                    mattermost_server=server,
                )
            return outcome
        except Exception as exc:
            db.rollback()
            logger.warning("Mattermost relay event failed exception_type=%s", type(exc).__name__)
            return RelayOutcome("failed", "worker_dispatch_failed")


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.MATTERMOST_RELAY_ENABLED:
        raise RuntimeError("Set MATTERMOST_RELAY_ENABLED=true to start the Mattermost relay worker.")
    server, _team_id, token = service_configuration(settings)
    factory = get_session_factory()
    delay = 1
    while True:
        try:
            bot_user_id = _service_account_user_id(server, token)
            ignored_user_ids = frozenset(filter(None, (
                bot_user_id,
                (settings.MATTERMOST_SWITCH_BOT_USER_ID or "").strip(),
            )))
            await run_authenticated_connection(
                server=server,
                token=token,
                session_factory=factory,
                settings=settings,
                ignored_user_ids=ignored_user_ids,
            )
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Mattermost event connection failed exception_type=%s retry_seconds=%s", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def _service_account_user_id(server: str, token: str) -> str:
    """Resolve the relay's own Mattermost identity through its authenticated API token."""
    current_user = _request("GET", server, "users/me", token=token)
    user_id = current_user.get("id") if isinstance(current_user, dict) else None
    if not isinstance(user_id, str) or not user_id.strip():
        raise MattermostError("mattermost_invalid_response", "Mattermost did not return the relay service account identity.")
    return user_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
