"""Explainable, deterministic invoice-to-bank reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Callable, Iterable
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from ..models import BankLine, Invoice, Match, MatchEvent
from .clarification_service import create_clarifications_for_missing_transactions
from ..schemas.reconciliation import (
    MissingInvoiceTransactionResponse,
    ReconciliationDecisionResponse,
    ReconciliationRunResponse,
)

VENDOR_MATCH_THRESHOLD = 0.90
DATE_WINDOW_BEFORE_DAYS = 7
DATE_WINDOW_AFTER_DAYS = 30
_RECONCILIATION_LOCK_KEY = int.from_bytes(b"LEDGRREC", "big", signed=True)
_STATUS_NAMES = (
    "matched",
    "amount_mismatch",
    "missing_payment",
    "possible_duplicate",
    "needs_review",
)


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """One invoice-level decision and any bank lines supporting it."""

    invoice_id: UUID
    status: str
    bank_line_ids: tuple[UUID, ...]
    candidate_bank_line_ids: tuple[UUID, ...]
    vendor_similarity: float | None
    explanation: str
    match_ids: tuple[UUID, ...] = ()


def normalize_vendor_name(name: str | None) -> str:
    """Lowercase, remove punctuation, collapse spaces, and strip legal suffixes."""
    if not name:
        return ""
    words = "".join(character if character.isalnum() else " " for character in name.casefold())
    tokens = re.sub(r"\s+", " ", words).strip().split()
    while tokens:
        if len(tokens) >= 2 and tuple(tokens[-2:]) in {
            ("pvt", "ltd"),
            ("pvt", "limited"),
            ("private", "limited"),
        }:
            del tokens[-2:]
        elif tokens[-1] in {"ltd", "limited", "pvt", "private"}:
            tokens.pop()
        else:
            break
    return " ".join(tokens)


def vendor_similarity(left: str | None, right: str | None) -> float:
    """Return a deterministic SequenceMatcher similarity for normalized names."""
    normalized_left = normalize_vendor_name(left)
    normalized_right = normalize_vendor_name(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right, autojunk=False).ratio()


def _invoice_vendor_variants(invoice: object) -> set[str]:
    names: set[str] = set()
    vendor_name = getattr(invoice, "vendor_name", None)
    if vendor_name:
        names.add(vendor_name)

    expected = getattr(invoice, "expected_vendor", None)
    if expected is not None and getattr(expected, "active", True):
        expected_name = getattr(expected, "name", None)
        if expected_name:
            names.add(expected_name)
        normalized_name = getattr(expected, "normalized_name", None)
        if normalized_name:
            names.add(normalized_name)
        for alias in getattr(expected, "aliases", None) or []:
            if isinstance(alias, str) and alias.strip():
                names.add(alias)
    return {normalized for name in names if (normalized := normalize_vendor_name(name))}


def _invoice_bank_similarity(invoice_variants: set[str], description: str) -> float:
    bank_name = normalize_vendor_name(description)
    if not bank_name or not invoice_variants:
        return 0.0
    return max(
        SequenceMatcher(None, invoice_name, bank_name, autojunk=False).ratio()
        for invoice_name in invoice_variants
    )


def _in_date_window(invoice_date: date, bank_date: date) -> bool:
    return (
        invoice_date - timedelta(days=DATE_WINDOW_BEFORE_DAYS)
        <= bank_date
        <= invoice_date + timedelta(days=DATE_WINDOW_AFTER_DAYS)
    )


def _decision(
    invoice: object,
    status_name: str,
    candidate_ids: Iterable[UUID],
    similarity: float | None,
    explanation: str,
    bank_line_ids: Iterable[UUID] = (),
) -> ReconciliationDecision:
    return ReconciliationDecision(
        invoice_id=invoice.id,
        status=status_name,
        bank_line_ids=tuple(sorted(set(bank_line_ids), key=str)),
        candidate_bank_line_ids=tuple(sorted(set(candidate_ids), key=str)),
        vendor_similarity=similarity,
        explanation=explanation,
    )


def evaluate_reconciliation(
    invoices: Iterable[object],
    bank_lines: Iterable[object],
    existing_matches: Iterable[object] = (),
) -> list[ReconciliationDecision]:
    """Evaluate all invoices without I/O or side effects, using integer paise."""
    positive_bank_lines = [line for line in bank_lines if line.amount_paise > 0]
    owners_by_bank_line: dict[UUID, set[UUID]] = {}
    for existing in existing_matches:
        if existing.bank_line_id is not None:
            owners_by_bank_line.setdefault(existing.bank_line_id, set()).add(
                existing.invoice_id
            )

    provisional: list[ReconciliationDecision] = []
    for invoice in invoices:
        invoice_id = invoice.id
        total = invoice.total_amount_paise
        invoice_date = invoice.invoice_date
        vendor_variants = _invoice_vendor_variants(invoice)
        if total is None or invoice_date is None or not vendor_variants:
            missing_fields = [
                field_name
                for field_name, is_present in (
                    ("total_amount_paise", total is not None),
                    ("invoice_date", invoice_date is not None),
                    ("vendor_name", bool(vendor_variants)),
                )
                if not is_present
            ]
            provisional.append(
                _decision(
                    invoice,
                    "needs_review",
                    (),
                    None,
                    "Invoice is missing required extracted field(s): "
                    + ", ".join(missing_fields)
                    + ".",
                )
            )
            continue

        comparisons = []
        for bank_line in positive_bank_lines:
            similarity = _invoice_bank_similarity(vendor_variants, bank_line.description)
            in_window = _in_date_window(invoice_date, bank_line.transaction_date)
            exact_amount = invoice.total_amount_paise == bank_line.amount_paise
            comparisons.append((bank_line, similarity, in_window, exact_amount))

        exact_vendor_in_window = [
            item
            for item in comparisons
            if item[1] >= VENDOR_MATCH_THRESHOLD and item[2] and item[3]
        ]
        exact_vendor_outside_window = [
            item
            for item in comparisons
            if item[1] >= VENDOR_MATCH_THRESHOLD and not item[2] and item[3]
        ]
        vendor_in_window_amount_differs = [
            item
            for item in comparisons
            if item[1] >= VENDOR_MATCH_THRESHOLD and item[2] and not item[3]
        ]
        exact_amount_in_window = [
            item for item in comparisons if item[2] and item[3]
        ]

        weak_exact_amount_in_window = [
            item
            for item in exact_amount_in_window
            if item[1] < VENDOR_MATCH_THRESHOLD
        ]

        if len(exact_vendor_in_window) > 1:
            candidates = [item[0].id for item in exact_vendor_in_window]
            provisional.append(
                _decision(
                    invoice,
                    "possible_duplicate",
                    candidates,
                    max(item[1] for item in exact_vendor_in_window),
                    "Multiple positive bank debits have the exact invoice total, a vendor similarity of at least 90%, and dates within the allowed window; review for duplicate payment.",
                    bank_line_ids=candidates,
                )
            )
        elif len(exact_vendor_in_window) == 1 and weak_exact_amount_in_window:
            candidates = [item[0].id for item in exact_amount_in_window]
            provisional.append(
                _decision(
                    invoice,
                    "needs_review",
                    candidates,
                    max(item[1] for item in exact_amount_in_window),
                    "An exact vendor candidate and another exact-amount/date candidate below the 90% vendor threshold were found; human review is required.",
                )
            )
        elif len(exact_vendor_in_window) == 1:
            bank_line, similarity, _, _ = exact_vendor_in_window[0]
            provisional.append(
                _decision(
                    invoice,
                    "matched",
                    [bank_line.id],
                    similarity,
                    f"Exact amount match at {invoice.total_amount_paise} paise; vendor similarity {similarity:.1%}; transaction date {bank_line.transaction_date.isoformat()} is within the allowed window.",
                    bank_line_ids=[bank_line.id],
                )
            )
        elif exact_vendor_outside_window:
            candidates = [item[0].id for item in exact_vendor_outside_window]
            provisional.append(
                _decision(
                    invoice,
                    "needs_review",
                    candidates,
                    max(item[1] for item in exact_vendor_outside_window),
                    "Exact amount and vendor candidate found, but transaction date is outside the window of 7 days before through 30 days after the invoice date.",
                )
            )
        elif exact_amount_in_window:
            candidates = [item[0].id for item in exact_amount_in_window]
            provisional.append(
                _decision(
                    invoice,
                    "needs_review",
                    candidates,
                    max(item[1] for item in exact_amount_in_window),
                    "Exact amount and date candidate found, but vendor similarity is below the 90% automatic threshold; human review is required.",
                )
            )
        elif len(vendor_in_window_amount_differs) == 1:
            bank_line, similarity, _, _ = vendor_in_window_amount_differs[0]
            provisional.append(
                _decision(
                    invoice,
                    "amount_mismatch",
                    [bank_line.id],
                    similarity,
                    f"Vendor similarity is {similarity:.1%} and transaction date is within the allowed window, but invoice total {invoice.total_amount_paise} paise differs from bank debit {bank_line.amount_paise} paise.",
                    bank_line_ids=[bank_line.id],
                )
            )
        elif vendor_in_window_amount_differs:
            candidates = [item[0].id for item in vendor_in_window_amount_differs]
            provisional.append(
                _decision(
                    invoice,
                    "needs_review",
                    candidates,
                    max(item[1] for item in vendor_in_window_amount_differs),
                    "Multiple vendor and date candidates have amounts different from the invoice; review is required.",
                )
            )
        else:
            provisional.append(
                _decision(
                    invoice,
                    "missing_payment",
                    (),
                    None,
                    "No positive bank debit matched the invoice amount and vendor within the applicable date window.",
                )
            )

    # A candidate bank line shared by multiple invoices is ambiguous, so none
    # of those invoices may claim it automatically.
    candidate_owners: dict[UUID, set[UUID]] = {}
    for decision in provisional:
        for candidate_id in decision.candidate_bank_line_ids:
            candidate_owners.setdefault(candidate_id, set()).add(decision.invoice_id)

    final: list[ReconciliationDecision] = []
    for decision in provisional:
        shared_candidates = [
            candidate_id
            for candidate_id in decision.candidate_bank_line_ids
            if len(candidate_owners[candidate_id]) > 1
        ]
        owned_by_other = [
            candidate_id
            for candidate_id in decision.candidate_bank_line_ids
            if owners_by_bank_line.get(candidate_id, set()) - {decision.invoice_id}
        ]
        if shared_candidates or owned_by_other:
            reasons = []
            if shared_candidates:
                reasons.append("candidate bank transaction is plausible for multiple invoices")
            if owned_by_other:
                reasons.append("candidate bank transaction is already assigned to another invoice")
            final.append(
                replace(
                    decision,
                    status="needs_review",
                    bank_line_ids=(),
                    explanation=(
                        "Human review required: "
                        + " and ".join(reasons)
                        + ". Candidate bank line IDs: "
                        + ", ".join(str(candidate_id) for candidate_id in decision.candidate_bank_line_ids)
                        + "."
                    ),
                )
            )
        else:
            final.append(decision)
    return final


def _notes_for(decision: ReconciliationDecision) -> str:
    notes = decision.explanation
    if decision.candidate_bank_line_ids:
        notes += " Candidate bank line IDs: " + ", ".join(
            str(candidate_id) for candidate_id in decision.candidate_bank_line_ids
        )
        notes += "."
    return notes[:1000]


def _decision_response(decision: ReconciliationDecision) -> ReconciliationDecisionResponse:
    return ReconciliationDecisionResponse(
        invoice_id=str(decision.invoice_id),
        match_ids=[str(match_id) for match_id in decision.match_ids],
        status=decision.status,  # type: ignore[arg-type]
        bank_line_ids=[str(bank_line_id) for bank_line_id in decision.bank_line_ids],
        candidate_bank_line_ids=[
            str(bank_line_id) for bank_line_id in decision.candidate_bank_line_ids
        ],
        vendor_similarity=decision.vendor_similarity,
        explanation=decision.explanation,
    )


def find_unmatched_bank_transactions(
    bank_lines: Iterable[object], matches: Iterable[object]
) -> list[MissingInvoiceTransactionResponse]:
    """List positive bank lines absent from links and structured candidates."""
    represented_ids: set[UUID] = set()
    for match in matches:
        if match.bank_line_id is not None:
            represented_ids.add(match.bank_line_id)
        for candidate_id in getattr(match, "candidate_bank_line_ids", None) or []:
            try:
                represented_ids.add(UUID(str(candidate_id)))
            except (ValueError, TypeError, AttributeError):
                continue

    return [
        MissingInvoiceTransactionResponse(
            bank_line_id=str(bank_line.id),
            description=bank_line.description,
            transaction_date=bank_line.transaction_date,
            amount_paise=bank_line.amount_paise,
            explanation=(
                "Positive bank debit is not linked to an invoice match or listed "
                "as a reconciliation candidate."
            ),
        )
        for bank_line in sorted(bank_lines, key=lambda row: (row.transaction_date, str(row.id)))
        if bank_line.amount_paise > 0 and bank_line.id not in represented_ids
    ]


def _confidence_for(decision: ReconciliationDecision) -> Decimal | None:
    if decision.status == "matched" and decision.vendor_similarity is not None:
        return Decimal(str(round(decision.vendor_similarity, 4)))
    return None


def _record_change_event(
    db: Session,
    match: Match,
    *,
    new_status: str,
    new_bank_line_id: UUID | None,
    new_notes: str,
    new_candidate_bank_line_ids: list[str],
    new_decision_source: str,
    new_confidence: Decimal | None,
    changed_at: datetime,
) -> None:
    """Append a full before/after snapshot immediately before a state change."""
    db.add(
        MatchEvent(
            id=uuid4(),
            business_id=match.business_id,
            match_id=match.id,
            invoice_id=match.invoice_id,
            previous_bank_line_id=match.bank_line_id,
            previous_status=match.status,
            previous_notes=match.notes,
            previous_candidate_bank_line_ids=list(
                match.candidate_bank_line_ids or []
            ),
            previous_decision_source=match.decision_source,
            previous_confidence=match.confidence,
            previous_updated_at=match.updated_at,
            new_bank_line_id=new_bank_line_id,
            new_status=new_status,
            new_notes=new_notes,
            new_candidate_bank_line_ids=list(new_candidate_bank_line_ids),
            new_decision_source=new_decision_source,
            new_confidence=new_confidence,
            changed_at=changed_at,
        )
    )


def _update_match_with_audit(
    db: Session,
    match: Match,
    *,
    status_name: str,
    bank_line_id: UUID | None,
    notes: str,
    candidate_bank_line_ids: list[str],
    decision_source: str,
    confidence: Decimal | None,
) -> bool:
    """Update a decision only after preserving its prior state in MatchEvent."""
    changed = any(
        (
            match.status != status_name,
            match.bank_line_id != bank_line_id,
            match.notes != notes,
            list(match.candidate_bank_line_ids or []) != candidate_bank_line_ids,
            match.decision_source != decision_source,
            match.confidence != confidence,
        )
    )
    if not changed:
        return False

    changed_at = datetime.now(timezone.utc)
    _record_change_event(
        db,
        match,
        new_status=status_name,
        new_bank_line_id=bank_line_id,
        new_notes=notes,
        new_candidate_bank_line_ids=candidate_bank_line_ids,
        new_decision_source=decision_source,
        new_confidence=confidence,
        changed_at=changed_at,
    )
    match.status = status_name
    match.bank_line_id = bank_line_id
    match.notes = notes
    match.candidate_bank_line_ids = list(candidate_bank_line_ids)
    match.decision_source = decision_source
    match.confidence = confidence
    match.updated_at = changed_at
    return True


def _human_decision(
    invoice_id: UUID, matches: list[Match], allowed_bank_line_ids: set[UUID] | None = None,
) -> ReconciliationDecision:
    """Build a read-only response for human-owned decisions."""
    valid_statuses = set(_STATUS_NAMES)
    statuses = {match.status for match in matches}
    status_name = (
        next(iter(statuses))
        if len(statuses) == 1 and next(iter(statuses)) in valid_statuses
        else "needs_review"
    )
    match_ids = tuple(match.id for match in matches)
    bank_line_ids = tuple(
        sorted(
            {match.bank_line_id for match in matches if match.bank_line_id is not None},
            key=str,
        )
    )
    candidate_ids: set[UUID] = set(bank_line_ids)
    for match in matches:
        for candidate_id in match.candidate_bank_line_ids or []:
            try:
                candidate_uuid = UUID(str(candidate_id))
                if allowed_bank_line_ids is None or candidate_uuid in allowed_bank_line_ids:
                    candidate_ids.add(candidate_uuid)
            except (ValueError, TypeError, AttributeError):
                continue
    notes = " ".join(note for match in matches if (note := match.notes))
    if any(match.status not in valid_statuses for match in matches):
        notes = (
            f"Legacy status retained without automation: {', '.join(sorted(statuses))}. "
            + notes
        )
    explanation = "Human-owned match decision preserved unchanged."
    if notes:
        explanation += " " + notes
    similarities = [float(match.confidence) for match in matches if match.confidence is not None]
    return ReconciliationDecision(
        invoice_id=invoice_id,
        status=status_name,
        bank_line_ids=bank_line_ids,
        candidate_bank_line_ids=tuple(sorted(candidate_ids, key=str)),
        vendor_similarity=max(similarities) if similarities else None,
        explanation=explanation,
        match_ids=match_ids,
    )


def run_reconciliation(
    db: Session, business_id: UUID,
    authorization_check: Callable[[Session], None] | None = None,
) -> ReconciliationRunResponse:
    """Evaluate and persist one current decision per invoice atomically."""
    try:
        with db.begin():
            if authorization_check is not None:
                authorization_check(db)
            # Serialize runs so concurrent calls cannot select and insert the
            # same candidate before the bank_line_id unique constraint applies.
            db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _RECONCILIATION_LOCK_KEY},
            )
            invoices = db.scalars(
                select(Invoice).where(Invoice.business_id == business_id)
                .options(selectinload(Invoice.expected_vendor))
            ).all()
            bank_lines = db.scalars(
                select(BankLine).where(
                    BankLine.business_id == business_id,
                    BankLine.amount_paise > 0,
                )
            ).all()
            existing_matches = db.scalars(select(Match).where(Match.business_id == business_id)).all()
            decisions = evaluate_reconciliation(invoices, bank_lines, existing_matches)
            allowed_bank_line_ids = {row.id for row in bank_lines}

            matches_by_invoice: dict[UUID, list[Match]] = {}
            for match in existing_matches:
                matches_by_invoice.setdefault(match.invoice_id, []).append(match)
            response_decisions: list[ReconciliationDecision] = []

            for decision in decisions:
                existing_for_invoice = matches_by_invoice.get(decision.invoice_id, [])
                human_owned = [
                    row for row in existing_for_invoice
                    if row.decision_source != "automated"
                ]
                if human_owned:
                    response_decisions.append(
                        _human_decision(decision.invoice_id, human_owned, allowed_bank_line_ids)
                    )
                    continue

                candidate_ids = [
                    str(candidate_id)
                    for candidate_id in decision.candidate_bank_line_ids
                ]
                notes = _notes_for(decision)
                confidence = _confidence_for(decision)
                desired_lines: tuple[UUID | None, ...] = (
                    tuple(decision.bank_line_ids)
                    if decision.bank_line_ids
                    else (None,)
                )
                selected_matches: list[Match] = []
                remaining_existing = list(existing_for_invoice)

                for bank_line_id in desired_lines:
                    match = next(
                        (
                            row
                            for row in remaining_existing
                            if row.bank_line_id == bank_line_id
                        ),
                        None,
                    )
                    if match is None and bank_line_id is not None:
                        match = next(
                            (row for row in remaining_existing if row.bank_line_id is None),
                            None,
                        )
                    if match is None:
                        match = Match(
                            id=uuid4(),
                            business_id=business_id,
                            invoice_id=decision.invoice_id,
                            bank_line_id=bank_line_id,
                            status=decision.status,
                            confidence=confidence,
                            notes=notes,
                            candidate_bank_line_ids=list(candidate_ids),
                            decision_source="automated",
                            updated_at=datetime.now(timezone.utc),
                        )
                        db.add(match)
                    else:
                        remaining_existing.remove(match)
                        _update_match_with_audit(
                            db,
                            match,
                            status_name=decision.status,
                            bank_line_id=bank_line_id,
                            notes=notes,
                            candidate_bank_line_ids=candidate_ids,
                            decision_source="automated",
                            confidence=confidence,
                        )

                    selected_matches.append(match)

                # Keep superseded source references intact and audit every
                # changed automated decision before marking it for review.
                for stale_match in remaining_existing:
                    stale_note = (
                        "Previously recorded bank-line association retained for audit; "
                        "it is not part of the current decision. "
                        + decision.explanation
                    )
                    _update_match_with_audit(
                        db,
                        stale_match,
                        status_name="needs_review",
                        bank_line_id=stale_match.bank_line_id,
                        notes=stale_note[:1000],
                        candidate_bank_line_ids=candidate_ids,
                        decision_source="automated",
                        confidence=None,
                    )

                response_decisions.append(
                    replace(
                        decision,
                        match_ids=tuple(match.id for match in selected_matches),
                    )
                )

            db.flush()
            persisted_matches = db.scalars(select(Match).where(Match.business_id == business_id)).all()
            missing_invoice_transactions = find_unmatched_bank_transactions(
                bank_lines, persisted_matches
            )
            create_clarifications_for_missing_transactions(
                db, missing_invoice_transactions, business_id
            )
    except SQLAlchemyError:
        db.rollback()
        raise

    counts = {status_name: 0 for status_name in _STATUS_NAMES}
    for decision in response_decisions:
        counts[decision.status] += 1

    return ReconciliationRunResponse(
        total_invoices=len(response_decisions),
        matched=counts["matched"],
        amount_mismatch=counts["amount_mismatch"],
        missing_payment=counts["missing_payment"],
        possible_duplicate=counts["possible_duplicate"],
        needs_review=counts["needs_review"],
        decisions=[_decision_response(decision) for decision in response_decisions],
        missing_invoice=len(missing_invoice_transactions),
        missing_invoice_transactions=missing_invoice_transactions,
    )
