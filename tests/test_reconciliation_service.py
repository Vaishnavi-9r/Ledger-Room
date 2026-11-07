"""Deterministic reconciliation behavior tests; no database connection used."""

import unittest
from contextlib import nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import UniqueConstraint
from fastapi import HTTPException

from backend.app.models import (
    BankLine,
    ChaseEvent,
    ClarificationRequest,
    Invoice,
    Match,
    MatchEvent,
)
from backend.app.api.clarifications import get_clarification
from backend.app.services.clarification_service import respond_to_clarification as _respond_to_clarification
from backend.app.services.reconciliation_service import (
    VENDOR_MATCH_THRESHOLD,
    evaluate_reconciliation,
    normalize_vendor_name,
    run_reconciliation as _run_reconciliation,
    vendor_similarity,
)

TEST_BUSINESS_ID = uuid4()


def run_reconciliation(db):
    return _run_reconciliation(db, TEST_BUSINESS_ID)


def respond_to_clarification(db, request_id, owner_response):
    return _respond_to_clarification(db, TEST_BUSINESS_ID, request_id, owner_response)


def make_invoice(
    vendor: str = "Sri Venkateswara Traders Pvt Ltd",
    total_paise: int | None = 473180,
    invoice_date: date | None = date(2026, 11, 3),
    expected_vendor: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        vendor_name=vendor,
        total_amount_paise=total_paise,
        invoice_date=invoice_date,
        expected_vendor=expected_vendor,
    )


def make_bank_line(
    description: str = "SRI VENKATESWARA TRADERS",
    amount_paise: int = 473180,
    transaction_date: date = date(2026, 11, 3),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        description=description,
        amount_paise=amount_paise,
        transaction_date=transaction_date,
    )


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeSession:
    """Small in-memory Session double for idempotent persistence behavior."""

    def __init__(self, invoices, bank_lines):
        self.invoices = list(invoices)
        self.bank_lines = list(bank_lines)
        self.matches = []
        self.events = []
        self.clarifications = []
        self.chase_events = []

    def begin(self):
        return nullcontext()

    def execute(self, statement, params=None):
        return None

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Invoice:
            return _ScalarResult(self.invoices)
        if entity is BankLine:
            return _ScalarResult(
                line for line in self.bank_lines if line.amount_paise > 0
            )
        if entity is Match:
            return _ScalarResult(self.matches)
        if entity is MatchEvent:
            return _ScalarResult(self.events)
        if entity is ClarificationRequest:
            return _ScalarResult(self.clarifications)
        raise AssertionError(f"Unexpected query entity: {entity}")

    def add(self, instance):
        if isinstance(instance, Match):
            self.matches.append(instance)
        elif isinstance(instance, MatchEvent):
            self.events.append(instance)
        elif isinstance(instance, ClarificationRequest):
            self.clarifications.append(instance)
        elif isinstance(instance, ChaseEvent):
            self.chase_events.append(instance)

    def get(self, entity, identity, **kwargs):
        if entity is ClarificationRequest:
            return next(
                (item for item in self.clarifications if item.id == identity), None
            )
        return None

    def scalar(self, statement):
        if statement.column_descriptions[0]["entity"] is ClarificationRequest:
            values = self.clarifications.values() if isinstance(self.clarifications, dict) else self.clarifications
            return next(iter(values), None)
        return None

    def flush(self):
        return None

    def rollback(self):
        return None


class ReconciliationServiceTests(unittest.TestCase):
    def test_vendor_normalization_removes_punctuation_and_business_suffix(self):
        self.assertEqual(
            normalize_vendor_name("  SRI Venkateswara Traders, Pvt. Ltd. "),
            "sri venkateswara traders",
        )
        self.assertEqual(
            normalize_vendor_name("Sri Venkateswara Traders Private Limited"),
            "sri venkateswara traders",
        )

    def test_fuzzy_vendor_matching_uses_ninety_percent_threshold(self):
        close = vendor_similarity("Acme Trading Pvt Ltd", "Acme Tradin")
        weak = vendor_similarity("Sri Venkateswara Traders", "Unrelated Hardware")
        self.assertGreaterEqual(close, VENDOR_MATCH_THRESHOLD)
        self.assertLess(weak, VENDOR_MATCH_THRESHOLD)

        close_result = evaluate_reconciliation(
            [make_invoice(vendor="Acme Trading Pvt Ltd")],
            [make_bank_line(description="Acme Tradin")],
        )[0]
        weak_result = evaluate_reconciliation(
            [make_invoice(vendor="Sri Venkateswara Traders")],
            [make_bank_line(description="Unrelated Hardware")],
        )[0]
        self.assertEqual(close_result.status, "matched")
        self.assertEqual(weak_result.status, "needs_review")

    def test_exact_amount_vendor_and_date_are_matched(self):
        invoice = make_invoice()
        bank_line = make_bank_line()

        result = evaluate_reconciliation([invoice], [bank_line])[0]

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.bank_line_ids, (bank_line.id,))
        self.assertIn("Exact amount match at 473180 paise", result.explanation)

    def test_weak_additional_exact_amount_candidate_forces_review(self):
        invoice = make_invoice()
        strong = make_bank_line()
        weak = make_bank_line(description="Unrelated Hardware")

        result = evaluate_reconciliation([invoice], [strong, weak])[0]

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(
            set(result.candidate_bank_line_ids), {strong.id, weak.id}
        )
        self.assertEqual(result.bank_line_ids, ())

    def test_vendor_and_date_match_with_different_amount_needs_amount_mismatch(self):
        result = evaluate_reconciliation(
            [make_invoice(total_paise=473180)],
            [make_bank_line(amount_paise=473179)],
        )[0]

        self.assertEqual(result.status, "amount_mismatch")
        self.assertIn("differs from bank debit", result.explanation)

    def test_exact_vendor_and_amount_outside_date_window_needs_review(self):
        result = evaluate_reconciliation(
            [make_invoice(invoice_date=date(2026, 11, 3))],
            [make_bank_line(transaction_date=date(2026, 12, 4))],
        )[0]

        self.assertEqual(result.status, "needs_review")
        self.assertIn("outside the window", result.explanation)

    def test_missing_payment_is_reported_when_no_positive_candidate_exists(self):
        result = evaluate_reconciliation([make_invoice()], [])[0]

        self.assertEqual(result.status, "missing_payment")
        self.assertEqual(result.bank_line_ids, ())

    def test_multiple_exact_vendor_debits_are_possible_duplicate(self):
        first = make_bank_line()
        second = make_bank_line(transaction_date=date(2026, 11, 4))

        result = evaluate_reconciliation([make_invoice()], [first, second])[0]

        self.assertEqual(result.status, "possible_duplicate")
        self.assertEqual(set(result.bank_line_ids), {first.id, second.id})

    def test_expected_vendor_alias_is_used_and_competing_invoices_are_ambiguous(self):
        expected = SimpleNamespace(
            name="S.V. Traders Private Limited",
            normalized_name="s v traders",
            aliases=["Sri Venkateswara Traders"],
            active=True,
        )
        first = make_invoice(vendor="S V Traders", expected_vendor=expected)
        second = make_invoice(vendor="S V Traders", expected_vendor=expected)
        bank_line = make_bank_line()

        results = evaluate_reconciliation([first, second], [bank_line])

        self.assertEqual([item.status for item in results], ["needs_review"] * 2)
        self.assertTrue(all(not item.bank_line_ids for item in results))
        self.assertTrue(all(bank_line.id in item.candidate_bank_line_ids for item in results))

    def test_existing_bank_line_assignment_blocks_a_second_invoice(self):
        first = make_invoice()
        second = make_invoice()
        bank_line = make_bank_line()
        existing = SimpleNamespace(
            invoice_id=first.id, bank_line_id=bank_line.id, status="matched"
        )

        results = evaluate_reconciliation(
            [second], [bank_line], existing_matches=[existing]
        )

        self.assertEqual(results[0].status, "needs_review")
        self.assertEqual(results[0].bank_line_ids, ())

    def test_repeated_reconciliation_reuses_match_record(self):
        invoice = make_invoice()
        bank_line = make_bank_line()
        session = _FakeSession([invoice], [bank_line])

        first_run = run_reconciliation(session)
        first_match_id = first_run.decisions[0].match_ids[0]
        second_run = run_reconciliation(session)

        self.assertEqual(len(session.matches), 1)
        self.assertEqual(second_run.decisions[0].match_ids, [first_match_id])
        self.assertEqual(second_run.matched, 1)
        self.assertEqual(session.events, [])

    def test_changed_decision_retains_previous_status_notes_ids_and_timestamp(self):
        invoice = make_invoice(total_paise=473180)
        bank_line = make_bank_line(amount_paise=473180)
        session = _FakeSession([invoice], [bank_line])
        first_run = run_reconciliation(session)
        match = session.matches[0]
        original_timestamp = match.updated_at
        original_notes = match.notes
        original_invoice_id = match.invoice_id
        original_bank_line_id = match.bank_line_id

        invoice.total_amount_paise = 473181
        changed_run = run_reconciliation(session)

        self.assertEqual(changed_run.amount_mismatch, 1)
        self.assertEqual(len(session.events), 1)
        event = session.events[0]
        self.assertEqual(event.previous_status, "matched")
        self.assertEqual(event.previous_notes, original_notes)
        self.assertEqual(event.invoice_id, original_invoice_id)
        self.assertEqual(event.previous_bank_line_id, original_bank_line_id)
        self.assertEqual(event.previous_updated_at, original_timestamp)
        self.assertEqual(event.new_status, "amount_mismatch")
        self.assertEqual(first_run.decisions[0].match_ids[0], str(match.id))

    def test_unchanged_rerun_after_change_does_not_duplicate_audit_event(self):
        invoice = make_invoice(total_paise=473180)
        bank_line = make_bank_line(amount_paise=473180)
        session = _FakeSession([invoice], [bank_line])
        run_reconciliation(session)
        invoice.total_amount_paise = 473181

        run_reconciliation(session)
        run_reconciliation(session)

        self.assertEqual(len(session.events), 1)

    def test_human_reviewed_match_is_not_replaced_by_automation(self):
        invoice = make_invoice(total_paise=473180)
        bank_line = make_bank_line(amount_paise=473180)
        human_match = Match(
            id=uuid4(),
            invoice_id=invoice.id,
            bank_line_id=bank_line.id,
            status="matched",
            notes="Approved by reviewer after receipt check.",
            candidate_bank_line_ids=[str(bank_line.id)],
            decision_source="human",
            updated_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
        )
        session = _FakeSession([invoice], [bank_line])
        session.matches.append(human_match)
        invoice.total_amount_paise = 473181

        result = run_reconciliation(session)

        self.assertEqual(human_match.status, "matched")
        self.assertEqual(human_match.notes, "Approved by reviewer after receipt check.")
        self.assertEqual(human_match.bank_line_id, bank_line.id)
        self.assertEqual(result.decisions[0].status, "matched")
        self.assertIn("Human-owned", result.decisions[0].explanation)
        self.assertEqual(session.events, [])

    def test_ambiguous_candidate_ids_are_persisted_completely(self):
        invoice = make_invoice()
        first = make_bank_line()
        second = make_bank_line(transaction_date=date(2026, 11, 4))
        session = _FakeSession([invoice], [first, second])

        result = run_reconciliation(session)

        expected_ids = sorted([str(first.id), str(second.id)])
        self.assertEqual(result.possible_duplicate, 1)
        self.assertEqual(len(session.matches), 2)
        self.assertTrue(
            all(match.candidate_bank_line_ids == expected_ids for match in session.matches)
        )

    def test_credits_are_excluded(self):
        credit = make_bank_line(amount_paise=-473180)

        result = evaluate_reconciliation([make_invoice()], [credit])[0]

        self.assertEqual(result.status, "missing_payment")
        self.assertEqual(result.candidate_bank_line_ids, ())

    def test_unmatched_positive_debit_is_reported_as_missing_invoice(self):
        bank_line = make_bank_line(
            description="UNKNOWN HARDWARE STORE", amount_paise=350000
        )
        result = run_reconciliation(_FakeSession([], [bank_line]))

        self.assertEqual(result.missing_invoice, 1)
        self.assertEqual(len(result.missing_invoice_transactions), 1)
        reported = result.missing_invoice_transactions[0]
        self.assertEqual(reported.status, "missing_invoice")
        self.assertEqual(reported.bank_line_id, str(bank_line.id))
        self.assertEqual(reported.description, "UNKNOWN HARDWARE STORE")
        self.assertEqual(reported.transaction_date, bank_line.transaction_date)
        self.assertEqual(reported.amount_paise, 350000)
        self.assertTrue(reported.explanation)

    def test_matched_debit_is_not_reported_as_missing_invoice(self):
        bank_line = make_bank_line()
        result = run_reconciliation(
            _FakeSession([make_invoice()], [bank_line])
        )

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.missing_invoice, 0)
        self.assertEqual(result.missing_invoice_transactions, [])

    def test_credit_is_not_reported_as_missing_invoice(self):
        credit = make_bank_line(description="CUSTOMER PAYMENT", amount_paise=-800000)
        result = run_reconciliation(_FakeSession([], [credit]))

        self.assertEqual(result.missing_invoice, 0)
        self.assertEqual(result.missing_invoice_transactions, [])

    def test_repeated_run_reports_unmatched_debit_without_creating_records(self):
        bank_line = make_bank_line(description="UNKNOWN HARDWARE STORE", amount_paise=350000)
        session = _FakeSession([], [bank_line])

        first = run_reconciliation(session)
        second = run_reconciliation(session)

        self.assertEqual(first.missing_invoice, second.missing_invoice)
        self.assertEqual(first.missing_invoice_transactions, second.missing_invoice_transactions)
        self.assertEqual(session.matches, [])
        self.assertEqual(session.events, [])
        self.assertEqual(len(session.clarifications), 1)
        self.assertEqual(len(session.chase_events), 1)
        self.assertEqual(
            first.missing_invoice_transactions[0].clarification_request_id,
            str(session.clarifications[0].id),
        )

    def test_unmatched_debit_creates_pending_clarification_and_creation_event(self):
        bank_line = make_bank_line(
            description="UNKNOWN HARDWARE STORE", amount_paise=350000
        )
        session = _FakeSession([], [bank_line])

        result = run_reconciliation(session)

        self.assertEqual(len(session.clarifications), 1)
        request = session.clarifications[0]
        self.assertEqual(request.bank_line_id, bank_line.id)
        self.assertEqual(request.status, "pending")
        self.assertIn("UNKNOWN HARDWARE STORE", request.question)
        self.assertEqual(result.missing_invoice_transactions[0].clarification_request_id, str(request.id))
        self.assertEqual(session.chase_events[0].event_type, "clarification_created")
        self.assertEqual(session.chase_events[0].bank_line_id, bank_line.id)
        self.assertEqual(session.chase_events[0].clarification_request_id, request.id)

    def test_matched_debit_does_not_create_clarification(self):
        bank_line = make_bank_line()
        session = _FakeSession([make_invoice()], [bank_line])

        result = run_reconciliation(session)

        self.assertEqual(result.matched, 1)
        self.assertEqual(session.clarifications, [])
        self.assertEqual(session.chase_events, [])

    def test_credit_does_not_create_clarification(self):
        credit = make_bank_line(description="CUSTOMER PAYMENT", amount_paise=-800000)
        session = _FakeSession([], [credit])

        result = run_reconciliation(session)

        self.assertEqual(result.missing_invoice, 0)
        self.assertEqual(session.clarifications, [])
        self.assertEqual(session.chase_events, [])

    def test_owner_response_updates_only_target_request_and_appends_event(self):
        target_line = BankLine(
            id=uuid4(),
            transaction_date=date(2026, 11, 3),
            description="UNKNOWN HARDWARE STORE",
            amount_paise=350000,
            currency="INR",
        )
        other_line = BankLine(
            id=uuid4(),
            transaction_date=date(2026, 11, 4),
            description="OTHER STORE",
            amount_paise=100000,
            currency="INR",
        )
        target = ClarificationRequest(
            id=uuid4(),
            bank_line_id=target_line.id,
            bank_line=target_line,
            status="pending",
            question="What was this transaction for?",
        )
        other = ClarificationRequest(
            id=uuid4(),
            bank_line_id=other_line.id,
            bank_line=other_line,
            status="pending",
            question="What was this transaction for?",
        )
        session = _FakeSession([], [])
        session.clarifications.extend([target, other])
        creation_event = ChaseEvent(
            bank_line_id=target_line.id,
            clarification_request_id=target.id,
            event_type="clarification_created",
            details={"question": target.question},
        )
        session.chase_events.append(creation_event)

        updated = respond_to_clarification(
            session, target.id, "Office hardware for the new branch."
        )

        self.assertEqual(updated.id, target.id)
        self.assertEqual(updated.status, "answered")
        self.assertEqual(updated.owner_response, "Office hardware for the new branch.")
        self.assertEqual(other.status, "pending")
        self.assertEqual(len(session.chase_events), 2)
        self.assertIs(session.chase_events[0], creation_event)
        self.assertEqual(session.chase_events[0].event_type, "clarification_created")
        self.assertEqual(session.chase_events[0].details["question"], target.question)
        self.assertEqual(
            session.chase_events[1].event_type,
            "clarification_owner_responded",
        )
        self.assertEqual(session.chase_events[1].clarification_request_id, target.id)
        self.assertEqual(session.chase_events[1].details["owner_response"], updated.owner_response)

    def test_invalid_clarification_request_id_returns_404(self):
        with self.assertRaises(HTTPException) as context:
            get_clarification("not-a-uuid", _FakeSession([], []))

        self.assertEqual(context.exception.status_code, 404)

    def test_multiple_invoices_only_report_unrepresented_debits(self):
        first_invoice = make_invoice()
        second_invoice = make_invoice(
            vendor="ABC Stationery",
            total_paise=250000,
            invoice_date=date(2026, 11, 3),
        )
        first_payment = make_bank_line()
        second_payment = make_bank_line(
            description="ABC Stationery", amount_paise=250000
        )
        unmatched = make_bank_line(
            description="UNKNOWN HARDWARE STORE",
            amount_paise=350000,
            transaction_date=date(2026, 11, 4),
        )

        result = run_reconciliation(
            _FakeSession(
                [first_invoice, second_invoice],
                [first_payment, second_payment, unmatched],
            )
        )

        self.assertEqual(result.matched, 2)
        self.assertEqual(result.missing_invoice, 1)
        self.assertEqual(
            [row.bank_line_id for row in result.missing_invoice_transactions],
            [str(unmatched.id)],
        )

    def test_match_model_has_bank_line_uniqueness_safeguard(self):
        unique_names = {
            constraint.name
            for constraint in Match.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }

        self.assertIn("uq_matches_bank_line_id", unique_names)


if __name__ == "__main__":
    unittest.main()
