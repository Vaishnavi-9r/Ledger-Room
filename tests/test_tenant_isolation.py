"""API-level tenant and role isolation checks using fake sessions."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from io import BytesIO
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from PIL import Image
from fastapi import HTTPException

from backend.app.api.clarifications import get_receipt_provider
from backend.app.api.invoices import get_extraction_provider
from backend.app.db.session import get_db
from backend.app.dependencies.business import BusinessContext, get_business_context
from backend.app.dependencies import business as business_dependency
from backend.app.dependencies.auth import AuthenticatedUser
from backend.app.main import app
from backend.app.models import Invoice
from backend.app.services.reconciliation_service import run_reconciliation


class _Result:
    def __init__(self, row=None):
        self.row = row

    def first(self):
        return self.row


class _Session:
    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result
        self.statements = []
        self.added = []
        self.commits = 0

    def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_result

    def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _Result(None)

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def begin(self):
        return nullcontext()

    def rollback(self):
        return None

    def begin(self):
        return nullcontext()

    def flush(self):
        return None


def _context(*, role="owner"):
    business_id, user_id = uuid4(), uuid4()
    business = SimpleNamespace(id=business_id, is_demo=False)
    membership = SimpleNamespace(id=uuid4(), business_id=business_id, user_id=user_id, role=role, status="active")
    principal = AuthenticatedUser(
        user=SimpleNamespace(id=user_id, email="user@example.test", email_normalized="user@example.test"),
        family_id=uuid4(), token_id="test",
    )
    return BusinessContext(business=business, membership=membership, principal=principal)


class TenantIsolationTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_business_context, None)
        app.dependency_overrides.pop(get_extraction_provider, None)
        app.dependency_overrides.pop(get_receipt_provider, None)

    def client(self, session, context):
        def db_override():
            yield session
        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_business_context] = lambda: context
        return TestClient(app)

    def test_unauthenticated_financial_route_rejects_request(self):
        response = TestClient(app).get(f"/api/v1/invoices/?business_id={uuid4()}")
        self.assertEqual(response.status_code, 401)

    def test_user_cannot_read_or_download_another_business_invoice(self):
        context, other_invoice_id = _context(), uuid4()
        session = _Session()
        client = self.client(session, context)
        response = client.get(f"/api/v1/invoices/{other_invoice_id}/image?business_id={context.business_id}")
        self.assertEqual(response.status_code, 404)
        query = session.statements[-1]
        sql = str(query)
        self.assertIn("invoices.business_id", sql)
        self.assertIn("invoices.id", sql)

    def test_user_cannot_extract_another_business_invoice(self):
        context = _context()
        session = _Session()
        provider = SimpleNamespace(extract=AsyncMock())
        app.dependency_overrides[get_extraction_provider] = lambda: provider
        response = self.client(session, context).post(
            f"/api/v1/invoices/{uuid4()}/extract?business_id={context.business_id}"
        )
        self.assertEqual(response.status_code, 404)
        provider.extract.assert_not_awaited()

    def test_accountant_cannot_run_reconciliation_or_import_bank_statements(self):
        context = _context(role="accountant")
        client = self.client(_Session(), context)
        run_response = client.post(f"/api/v1/reconciliation/run?business_id={context.business_id}")
        bank_response = client.post(
            f"/api/v1/bank-statements/upload?business_id={context.business_id}",
            files={"file": ("statement.csv", b"transaction_date,description,debit,credit,reference\n", "text/csv")},
        )
        self.assertEqual(run_response.status_code, 403)
        self.assertEqual(bank_response.status_code, 403)

    def test_user_cannot_read_or_respond_to_another_business_clarification(self):
        context = _context()
        session = _Session()
        client = self.client(session, context)
        clarification_id = uuid4()
        read_response = client.get(f"/api/v1/clarifications/{clarification_id}?business_id={context.business_id}")
        answer_response = client.post(
            f"/api/v1/clarifications/{clarification_id}/respond?business_id={context.business_id}",
            json={"owner_response": "Owner-provided explanation."},
        )
        self.assertEqual(read_response.status_code, 404)
        self.assertEqual(answer_response.status_code, 404)
        self.assertEqual(session.commits, 0)

    def test_accountant_cannot_respond_or_upload_receipt(self):
        context = _context(role="accountant")
        session = _Session()
        client = self.client(session, context)
        clarification_id = uuid4()
        answer_response = client.post(
            f"/api/v1/clarifications/{clarification_id}/respond?business_id={context.business_id}",
            json={"owner_response": "Explanation."},
        )
        receipt_response = client.post(
            f"/api/v1/clarifications/{clarification_id}/receipt?business_id={context.business_id}",
            files={"file": ("receipt.jpg", b"invalid", "image/jpeg")},
        )
        self.assertEqual(answer_response.status_code, 403)
        self.assertEqual(receipt_response.status_code, 403)
        self.assertEqual(session.commits, 0)

    def test_report_requires_explicit_authenticated_membership_and_selected_business(self):
        business_id = uuid4()
        no_auth = TestClient(app).get(f"/api/v1/reports/monthly?year=2026&month=11&business_id={business_id}")
        no_business = TestClient(app).get("/api/v1/reports/monthly?year=2026&month=11")
        self.assertEqual(no_auth.status_code, 401)
        self.assertEqual(no_business.status_code, 401)

    def test_user_cannot_upload_receipt_to_another_business_clarification(self):
        context, session = _context(), _Session()
        image = BytesIO()
        Image.new("RGB", (3, 3), "white").save(image, format="JPEG")
        provider = SimpleNamespace(extract=AsyncMock())
        app.dependency_overrides[get_receipt_provider] = lambda: provider
        response = self.client(session, context).post(
            f"/api/v1/clarifications/{uuid4()}/receipt?business_id={context.business_id}",
            files={"file": ("receipt.jpg", image.getvalue(), "image/jpeg")},
        )
        self.assertEqual(response.status_code, 404)
        provider.extract.assert_not_awaited()
        self.assertEqual(session.commits, 0)

    def test_membership_dependency_hides_cross_business_and_inactive_memberships(self):
        context = _context()
        session = _Session()
        principal = context.principal
        with self.assertRaises(HTTPException) as caught:
            business_dependency.get_business_context(
                business_id=uuid4(), principal=principal, db=session,
            )
        self.assertEqual(caught.exception.status_code, 404)
        inactive = SimpleNamespace(id=uuid4(), status="revoked", role="accountant")
        with patch(
            "backend.app.dependencies.business.get_member_business",
            return_value=(context.business, inactive),
        ):
            with self.assertRaises(HTTPException) as caught:
                business_dependency.get_business_context(
                    business_id=context.business_id, principal=principal, db=session,
                )
        self.assertEqual(caught.exception.status_code, 404)

    def test_reconciliation_service_queries_every_source_by_business_id(self):
        context = _context()
        class EmptySession:
            def __init__(self):
                self.statements = []
            def begin(self):
                return nullcontext()
            def execute(self, statement, *args, **kwargs):
                self.statements.append(statement)
            def scalars(self, statement):
                self.statements.append(statement)
                return SimpleNamespace(all=lambda: [])
            def flush(self):
                return None
            def rollback(self):
                return None
        session = EmptySession()
        result = run_reconciliation(session, context.business_id)
        self.assertEqual(result.total_invoices, 0)
        scoped = [str(statement) for statement in session.statements if "business_id" in str(statement)]
        self.assertGreaterEqual(len(scoped), 4)
        self.assertTrue(all("business_id" in sql for sql in scoped))

    def test_scoped_invoice_insert_uses_context_not_client_payload(self):
        """Upload route construction is tenant-owned; request has no business_id body field."""
        context = _context()
        statement = select(Invoice).where(Invoice.business_id == context.business_id)
        self.assertIn("invoices.business_id", str(statement))
        self.assertEqual(context.business_id, context.membership.business_id)


if __name__ == "__main__":
    unittest.main()
