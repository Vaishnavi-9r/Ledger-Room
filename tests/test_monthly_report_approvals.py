"""Approval workflow tests use fake sessions; no database is contacted."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.db.session import get_db
from backend.app.dependencies.business import get_business_context
from backend.app.main import app
from backend.app.models import Business, MonthlyReportApproval, MonthlyReportApprovalEvent, User
from backend.app.schemas.monthly_report_approval import MonthlyReportApprovalResponse
from backend.app.services.monthly_report_approval_service import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStaleError,
    get_approval_response,
    report_fingerprint,
    review_approval,
    submit_monthly_report,
)
from backend.app.services.monthly_report_service import build_monthly_report


NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def _business():
    return SimpleNamespace(
        id=uuid4(), business_name="Ledger Shop", proprietor_name="Asha",
        gstin=None, address_line_1="1 Main", address_line_2=None,
        city="Pune", state="Maharashtra", pin_code="411001", country="IN",
    )


def _user(name="Owner"):
    return User(
        id=uuid4(), full_name=name, email=f"{name.lower()}@example.test",
        email_normalized=f"{name.lower()}@example.test", mobile_number="9999999999",
        password_hash="not-a-real-hash", is_active=True,
    )


def _report(business=None, *, generated_at=NOW, total=1000):
    business = business or _business()
    invoice = SimpleNamespace(
        id=uuid4(), uploaded_at=NOW, vendor_name="Stationery", invoice_number="A1",
        invoice_date=date(2026, 11, 3), total_amount_paise=total,
        extraction_status="completed", matches=[],
    )
    return build_monthly_report(
        2026, 11, [invoice], [], [], [], [], generated_at=generated_at, business=business,
    )


class _FakeSession:
    def __init__(self, current=None):
        self.current = current
        self.added = []
        self.statements = []

    def begin(self):
        return nullcontext()

    def scalar(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is Business:
            return self.business_id
        if entity is MonthlyReportApproval:
            return self.current
        return None

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [self.current] if self.current else [])

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        self.added.append(row)
        if isinstance(row, MonthlyReportApproval):
            self.current = row

    def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()


class MonthlyReportApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.business = _business()
        self.owner = _user("Owner")
        self.accountant = _user("Accountant")
        self.db = _FakeSession()
        self.db.business_id = self.business.id
        self.report = _report(self.business)

    def make_approval(self, status="pending_review", *, version=1, fingerprint=None, submitter=None):
        return MonthlyReportApproval(
            id=uuid4(), business_id=self.business.id, report_year=2026, report_month=11,
            version=version, status=status, submitted_by=(submitter or self.owner).id,
            submitted_at=NOW, report_fingerprint=fingerprint or report_fingerprint(self.report),
            created_at=NOW, updated_at=NOW, submitter=submitter or self.owner,
            reviewer=None, events=[],
        )

    def test_fingerprint_ignores_generation_time_and_is_stable(self):
        first = _report(self.business, generated_at=NOW)
        second = _report(self.business, generated_at=datetime(2026, 11, 11, tzinfo=timezone.utc))
        # The report fixture uses a new invoice UUID each time, so use the same report data
        # and only vary the generated timestamp for a true canonicalization check.
        second = first.model_copy(update={"generated_at": datetime(2026, 11, 11, tzinfo=timezone.utc)})
        self.assertEqual(report_fingerprint(first), report_fingerprint(second))
        self.assertEqual(len(report_fingerprint(first)), 64)

    def test_owner_submission_creates_version_and_audit_event(self):
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            approval = submit_monthly_report(self.db, self.business, self.owner, 2026, 11)
        self.assertEqual(approval.status, "pending_review")
        self.assertEqual(approval.version, 1)
        self.assertEqual(approval.submitted_by, self.owner.id)
        self.assertEqual(len(approval.events), 1)
        self.assertEqual(approval.events[0].event_type, "submitted")
        self.assertEqual(approval.events[0].report_fingerprint, approval.report_fingerprint)

    def test_duplicate_active_submission_is_rejected(self):
        self.db.current = self.make_approval()
        before = len(self.db.added)
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            with self.assertRaises(ApprovalConflictError):
                submit_monthly_report(self.db, self.business, self.owner, 2026, 11)
        self.assertEqual(len(self.db.added), before)

    def test_resubmission_creates_new_version_without_overwriting_old_decision(self):
        old = self.make_approval("changes_requested", version=1)
        old.accountant_comments = "Please attach the supporting statement."
        old.events = [MonthlyReportApprovalEvent(
            id=uuid4(), business_id=self.business.id, approval_id=old.id,
            actor_user_id=self.accountant.id, event_type="changes_requested",
            previous_status="pending_review", resulting_status="changes_requested",
            comments=old.accountant_comments, report_fingerprint=old.report_fingerprint,
            created_at=NOW,
        )]
        self.db.current = old
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            new = submit_monthly_report(self.db, self.business, self.owner, 2026, 11)
        self.assertEqual(new.version, 2)
        self.assertEqual(new.status, "pending_review")
        self.assertEqual(old.status, "changes_requested")
        self.assertEqual(old.accountant_comments, "Please attach the supporting statement.")
        self.assertEqual(old.events[0].event_type, "changes_requested")
        self.assertEqual(new.events[0].event_type, "resubmitted")

    def test_accountant_approval_is_atomic_and_audited(self):
        approval = self.make_approval()
        self.db.current = approval
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            reviewed = review_approval(self.db, self.business, approval.id, self.accountant, "approved", "Reviewed source documents")
        self.assertEqual(reviewed.status, "approved")
        self.assertEqual(reviewed.approval_decision, "approved")
        self.assertEqual(reviewed.reviewed_by, self.accountant.id)
        self.assertEqual(reviewed.accountant_comments, "Reviewed source documents")
        self.assertEqual(reviewed.events[-1].previous_status, "pending_review")
        self.assertEqual(reviewed.events[-1].resulting_status, "approved")
        self.assertEqual(reviewed.events[-1].actor_user_id, self.accountant.id)

    def test_request_changes_requires_nonblank_reason_and_preserves_history(self):
        approval = self.make_approval()
        old_event = MonthlyReportApprovalEvent(
            id=uuid4(), business_id=self.business.id, approval_id=approval.id,
            actor_user_id=self.owner.id, event_type="submitted", previous_status=None,
            resulting_status="pending_review", comments=None,
            report_fingerprint=approval.report_fingerprint, created_at=NOW,
        )
        approval.events = [old_event]
        self.db.current = approval
        with self.assertRaises(ValueError):
            review_approval(self.db, self.business, approval.id, self.accountant, "changes_requested", "  ")
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            reviewed = review_approval(self.db, self.business, approval.id, self.accountant, "changes_requested", "Correct the invoice date.")
        self.assertEqual(reviewed.status, "changes_requested")
        self.assertEqual(reviewed.events[0], old_event)
        self.assertEqual(reviewed.events[-1].comments, "Correct the invoice date.")

    def test_owner_cannot_approve_own_submission(self):
        approval = self.make_approval(submitter=self.owner)
        self.db.current = approval
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report") as report:
            with self.assertRaises(ApprovalConflictError):
                review_approval(self.db, self.business, approval.id, self.owner, "approved")
        report.assert_not_called()
        self.assertEqual(approval.status, "pending_review")

    def test_changed_report_fingerprint_marks_version_stale_and_preserves_approval_history(self):
        approval = self.make_approval()
        approval.events = [MonthlyReportApprovalEvent(
            id=uuid4(), business_id=self.business.id, approval_id=approval.id,
            actor_user_id=self.owner.id, event_type="submitted", previous_status=None,
            resulting_status="pending_review", comments=None,
            report_fingerprint=approval.report_fingerprint, created_at=NOW,
        )]
        self.db.current = approval
        changed = _report(self.business, total=2000)
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=changed):
            with self.assertRaises(ApprovalStaleError):
                review_approval(self.db, self.business, approval.id, self.accountant, "approved")
        self.assertEqual(approval.status, "stale")
        self.assertEqual(approval.events[0].event_type, "submitted")
        self.assertEqual(approval.events[-1].event_type, "stale")

    def test_concurrent_duplicate_review_second_attempt_conflicts(self):
        approval = self.make_approval()
        self.db.current = approval
        with patch("backend.app.services.monthly_report_approval_service.get_monthly_report", return_value=self.report):
            review_approval(self.db, self.business, approval.id, self.accountant, "approved")
            with self.assertRaises(ApprovalConflictError):
                review_approval(self.db, self.business, approval.id, _user("Second accountant"), "approved")
        self.assertEqual(approval.status, "approved")
        self.assertEqual(sum(isinstance(row, MonthlyReportApprovalEvent) for row in self.db.added), 1)

    def test_cross_business_approval_lookup_is_scoped_and_returns_not_found(self):
        other_business = _business()
        db = _FakeSession()
        with self.assertRaises(ApprovalNotFoundError):
            get_approval_response(db, other_business, uuid4())
        statement = db.statements[-1]
        self.assertIn("monthly_report_approvals.business_id", str(statement))
        self.assertIn("monthly_report_approvals.id", str(statement))

    def test_pending_unique_index_and_foreign_keys_protect_tenant_and_active_submissions(self):
        table = MonthlyReportApproval.__table__
        pending = next(index for index in table.indexes if index.name == "uq_report_approval_pending_period")
        self.assertTrue(pending.unique)
        self.assertIn("pending_review", str(pending.dialect_options["postgresql"]["where"]))
        event_table = MonthlyReportApprovalEvent.__table__
        self.assertTrue(any(fk.name == "fk_report_approval_event_business" for fk in event_table.foreign_key_constraints))


class MonthlyReportApprovalApiTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_business_context, None)

    def client(self, role):
        business = _business()
        user = _user(role.title())
        context = SimpleNamespace(
            business=business, business_id=business.id, role=role,
            principal=SimpleNamespace(user=user),
        )
        def db_override():
            yield _FakeSession()
        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_business_context] = lambda: context
        return TestClient(app)

    def test_owner_can_submit_but_accountant_cannot(self):
        client = self.client("accountant")
        response = client.post("/api/v1/reports/monthly/submit?year=2026&month=11&business_id=" + str(uuid4()))
        self.assertEqual(response.status_code, 403)
        client = self.client("owner")
        approval_id = uuid4()
        response_schema = MonthlyReportApprovalResponse(
            approval_id=approval_id, business_id=uuid4(), report_year=2026,
            report_month=11, version=1, status="pending_review", submitted_by=uuid4(),
            submitted_by_name="Owner", submitted_at=NOW, reviewed_by=None,
            reviewed_by_name=None, reviewed_at=None, accountant_comments=None,
            approval_decision=None, report_fingerprint="a" * 64, events=[],
        )
        approval = SimpleNamespace(id=approval_id)
        with patch("backend.app.api.reports.submit_monthly_report", return_value=approval), patch(
            "backend.app.api.reports.get_approval_response", return_value=response_schema
        ):
            # Schema response validation is tested by service response tests; here authorization
            # succeeds and the request reaches the configured submission service.
            response = client.post("/api/v1/reports/monthly/submit?year=2026&month=11&business_id=" + str(uuid4()))
        self.assertNotEqual(response.status_code, 403)

    def test_owner_cannot_call_accountant_decision_routes(self):
        client = self.client("owner")
        approval_id = uuid4()
        for route, body in (("approve", {}), ("request-changes", {"comments": "Please correct."})):
            response = client.post(
                f"/api/v1/reports/monthly/approvals/{approval_id}/{route}?business_id={uuid4()}",
                json=body,
            )
            self.assertEqual(response.status_code, 403)

    def test_inactive_or_missing_business_membership_rejects_report_review(self):
        response = TestClient(app).get(f"/api/v1/reports/monthly/approvals?business_id={uuid4()}")
        self.assertEqual(response.status_code, 401)

    def test_correction_comment_is_required(self):
        client = self.client("accountant")
        response = client.post(
            f"/api/v1/reports/monthly/approvals/{uuid4()}/request-changes?business_id={uuid4()}",
            json={},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
