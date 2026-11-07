"""Business-isolated persistent shared room and constrained agent commands."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from uuid import uuid4
from uuid import UUID

from ..db.session import get_db
from ..dependencies.business import BusinessContext, get_business_context
from ..models import ConversationMessage, Invoice
from ..schemas.conversation import (
    ConversationAttachment,
    ConversationHistoryResponse,
    ConversationMessageCreate,
    ConversationMessageResponse,
)
from ..services.monthly_report_service import get_monthly_report
from ..services.reconciliation_service import run_reconciliation

router = APIRouter(prefix="/api/v1/conversation", tags=["Shared Conversation"])
_REPORT_COMMAND = re.compile(r"^/report(?:\s+(\d{4})-(\d{2}))?$", re.IGNORECASE)
_CLARIFICATION_RESPONSE = re.compile(r"^/respond\s+([0-9a-f-]{36})\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _response(row: ConversationMessage) -> ConversationMessageResponse:
    return ConversationMessageResponse(
        id=row.id, business_id=row.business_id, sender_user_id=row.sender_user_id,
        sender_role=row.sender_role, sender_name=row.sender_name, content=row.content,
        attachments=[ConversationAttachment.model_validate(item) for item in (row.attachments or [])],
        created_at=row.created_at,
    )


def _save_message(db: Session, context: BusinessContext, *, role: str, name: str,
                  content: str, attachments: list[dict] | None = None,
                  user_id=None) -> ConversationMessage:
    row = ConversationMessage(
        id=uuid4(), created_at=datetime.now(timezone.utc),
        business_id=context.business_id, sender_user_id=user_id, sender_role=role,
        sender_name=name, content=content, attachments=attachments or [],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _agent_reply(db: Session, context: BusinessContext, text: str) -> ConversationMessage:
    return _save_message(db, context, role="agent", name="LedgerRoom AI", content=text)


async def _handle_agent(db: Session, context: BusinessContext, message: ConversationMessage,
                        attachments: list[ConversationAttachment]) -> ConversationMessage:
    """Route explicit requests to trusted existing services; never infer financial matches."""
    text = message.content.strip()
    if text.casefold() in {"/help", "help", "what can you do?"}:
        return _agent_reply(db, context,
            "I can run the existing deterministic reconciliation (`/reconcile`), summarize a real monthly report (`/report` or `/report YYYY-MM`), list pending clarifications (`/clarifications`), record an owner response with `/respond <request-id> <response>`, and extract an attached invoice with the configured provider. I cannot make financial decisions from chat text.")

    if text.casefold() == "/reconcile":
        if context.role != "owner":
            return _agent_reply(db, context, "Reconciliation changes financial records and is owner-only in this workspace. Ask the business owner to run `/reconcile`.")
        try:
            result = run_reconciliation(db, context.business_id)
            return _agent_reply(db, context,
                f"The existing deterministic reconciliation service finished for {result.total_invoices} invoices: {result.matched} matched, {result.amount_mismatch} amount mismatches, {result.missing_payment} missing payments, {result.possible_duplicate} possible duplicates, and {result.needs_review} needing review. Review the source records before acting on any result.")
        except Exception:
            db.rollback()
            return _agent_reply(db, context, "Reconciliation could not be completed. The existing reconciliation service returned an error; no result has been assumed.")

    report_match = _REPORT_COMMAND.fullmatch(text)
    if report_match:
        year = int(report_match.group(1)) if report_match.group(1) else date.today().year
        month = int(report_match.group(2)) if report_match.group(2) else date.today().month
        if not 1 <= month <= 12 or not 1900 <= year <= 9998:
            return _agent_reply(db, context, "Use `/report YYYY-MM`, for example `/report 2026-10`.")
        try:
            report = get_monthly_report(db, year, month, context.business_id, context.business)
            summary = report.summary
            return _agent_reply(db, context,
                f"Monthly report for {year}-{month:02d}: {summary.total_invoices_received} invoices, {summary.total_bank_transactions} bank transactions, {summary.matched_payments} matched payments, {summary.amount_mismatches} amount mismatches, {summary.missing_payments} missing payments, {summary.possible_duplicates} possible duplicates, {summary.transactions_requiring_review} items needing review, and {summary.pending_clarifications} pending clarifications. These figures come from the existing business-scoped report.")
        except Exception:
            db.rollback()
            return _agent_reply(db, context, "The monthly report could not be loaded from the existing report service. I have not estimated any figures.")

    if text.casefold() == "/clarifications":
        from ..models import ClarificationRequest
        from sqlalchemy import func
        from sqlalchemy.orm import selectinload
        pending_rows = db.scalars(select(ClarificationRequest).where(
            ClarificationRequest.business_id == context.business_id,
            ClarificationRequest.status == "pending",
        ).options(selectinload(ClarificationRequest.bank_line)).order_by(
            ClarificationRequest.created_at.desc()).limit(5)).all()
        pending = db.scalar(select(func.count()).select_from(ClarificationRequest).where(
            ClarificationRequest.business_id == context.business_id,
            ClarificationRequest.status == "pending",
        )) or 0
        db.rollback()
        if not pending:
            return _agent_reply(db, context, "There are no pending clarification requests for this business.")
        details = " ".join(
            f"{index}. {row.question} (transaction: {row.bank_line.description}; request {row.id})."
            for index, row in enumerate(pending_rows, start=1)
        )
        return _agent_reply(db, context, f"There are {pending} pending clarification(s). Here are up to five from this business: {details} An owner can respond through the existing clarification workflow.")

    response_match = _CLARIFICATION_RESPONSE.fullmatch(text)
    if response_match:
        if context.role != "owner":
            return _agent_reply(db, context, "Only the business owner can submit clarification responses in the existing workflow.")
        from uuid import UUID
        from ..services.clarification_service import (
            ClarificationNotFoundError, ClarificationStateConflictError,
            respond_to_clarification,
        )
        try:
            request = respond_to_clarification(
                db, context.business_id, UUID(response_match.group(1)), response_match.group(2).strip(),
            )
            return _agent_reply(db, context, f"Your response was saved to clarification {request.id}. Its current status is {request.status}.")
        except ClarificationNotFoundError:
            db.rollback()
            return _agent_reply(db, context, "That clarification was not found in this business workspace.")
        except ClarificationStateConflictError as exc:
            db.rollback()
            return _agent_reply(db, context, f"That clarification can no longer be answered: {exc}")

    if attachments:
        extracted = 0
        notes = []
        if context.role != "owner" and any(item.kind == "invoice" for item in attachments):
            notes.append("Invoice extraction is owner-only in the existing API; ask the owner to process this attachment.")
        else:
            from .invoices import extract_uploaded_invoice, get_extraction_provider
            for attachment in attachments:
                if attachment.kind != "invoice" or attachment.invoice_id is None:
                    continue
                invoice = db.scalar(select(Invoice).where(
                    Invoice.id == attachment.invoice_id,
                    Invoice.business_id == context.business_id,
                ))
                if invoice is None:
                    notes.append("An attached invoice was not found in this business; it was not processed.")
                    continue
                try:
                    result = await extract_uploaded_invoice(
                        str(invoice.id), context=context, db=db,
                        provider=get_extraction_provider(),
                    )
                    if getattr(result, "status_code", None):
                        notes.append("Invoice extraction is not available; check the configured extraction provider.")
                    else:
                        extracted += 1
                        notes.append(result.message)
                except Exception:
                    db.rollback()
                    notes.append("Invoice extraction failed through the existing extraction service.")
        if extracted:
            notes.insert(0, f"Processed {extracted} attached invoice(s) using the existing extraction provider.")
        if not notes:
            notes.append("The bank statement was uploaded through the existing import service. Use `/reconcile` to evaluate invoices against the imported bank lines.")
        return _agent_reply(db, context, " ".join(notes))

    from ..services.agent_service import AgentNotConfiguredError, respond_to_chat
    try:
        answer = await respond_to_chat(
            business_name=context.business.business_name,
            user_name=context.principal.user.full_name,
            role=context.role,
            message=text,
        )
        return _agent_reply(db, context, answer)
    except AgentNotConfiguredError:
        return _agent_reply(db, context,
            "I received your message. I can take verified LedgerRoom actions with `/help`, `/reconcile`, `/report YYYY-MM`, or `/clarifications`. General AI chat is unavailable until GEMINI_API_KEY is configured; I will not invent financial results.")


@router.get("", response_model=ConversationHistoryResponse)
def get_conversation(
    before: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> ConversationHistoryResponse:
    statement = select(ConversationMessage).where(
        ConversationMessage.business_id == context.business_id,
    ).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(limit)
    if before:
        try:
            cursor_id = UUID(before)
        except (ValueError, TypeError, AttributeError):
            cursor_id = None
        cursor = db.scalar(select(ConversationMessage).where(
            ConversationMessage.id == cursor_id,
            ConversationMessage.business_id == context.business_id,
        )) if cursor_id else None
        if cursor is not None:
            statement = statement.where(ConversationMessage.created_at < cursor.created_at)
    rows = list(reversed(db.scalars(statement).all()))
    return ConversationHistoryResponse(business_id=context.business_id, messages=[_response(row) for row in rows])


@router.post("/messages", response_model=list[ConversationMessageResponse], status_code=201)
async def post_message(
    payload: ConversationMessageCreate,
    context: BusinessContext = Depends(get_business_context),
    db: Session = Depends(get_db),
) -> list[ConversationMessageResponse]:
    content = payload.content.strip()
    if not content and not payload.attachments:
        raise HTTPException(status_code=422, detail="Add a message or attach a document.")

    clean_attachments = []
    for attachment in payload.attachments:
        if attachment.kind == "invoice":
            if attachment.invoice_id is None:
                raise HTTPException(status_code=422, detail="Invoice attachments require an invoice ID.")
            exists = db.scalar(select(Invoice.id).where(
                Invoice.id == attachment.invoice_id, Invoice.business_id == context.business_id,
            ))
            if exists is None:
                raise HTTPException(status_code=404, detail="Attached invoice not found in this business.")
        clean_attachments.append(attachment.model_dump(mode="json"))

    try:
        user_message = _save_message(
            db, context, role=context.role, name=context.principal.user.full_name,
            content=content or "Shared a document.", attachments=clean_attachments,
            user_id=context.principal.user.id,
        )
        agent_message = await _handle_agent(db, context, user_message, payload.attachments)
        return [_response(user_message), _response(agent_message)]
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Conversation could not be saved.") from exc
