"""Shared room API checks using isolated fake sessions (no database required)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.db.session import get_db
from backend.app.dependencies.auth import AuthenticatedUser, get_current_auth
from backend.app.dependencies.business import BusinessContext, get_business_context
from backend.app.main import app
from backend.app.models import ConversationMessage


class _Session:
    def __init__(self):
        self.rows = []
        self.commit_count = 0
        self.rollback_count = 0

    def scalars(self, statement):
        # The SELECT must constrain the query to the active business.
        business_id = statement.compile().params.get("business_id_1")
        rows = [row for row in self.rows if row.business_id == business_id] if business_id else []
        return SimpleNamespace(all=lambda: rows)

    def scalar(self, _statement):
        return None

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        self.commit_count += 1

    def refresh(self, _row):
        return None

    def rollback(self):
        self.rollback_count += 1


def _context(role, business_id, user_id=None):
    user_id = user_id or uuid4()
    return BusinessContext(
        business=SimpleNamespace(id=business_id, business_name="Shared Co", is_demo=False),
        membership=SimpleNamespace(id=uuid4(), business_id=business_id, user_id=user_id,
                                   role=role, status="active"),
        principal=AuthenticatedUser(
            user=SimpleNamespace(id=user_id, full_name=role.title(), email=f"{role}@example.test"),
            family_id=uuid4(), token_id="test-token",
        ),
    )


class ConversationApiTests(unittest.TestCase):
    def tearDown(self):
        for dependency in (get_db, get_current_auth, get_business_context):
            app.dependency_overrides.pop(dependency, None)

    def install(self, db, context):
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_business_context] = lambda: context

    def test_owner_and_accountant_share_persistent_messages_and_agent_replies(self):
        db = _Session()
        business_id = uuid4()
        owner = _context("owner", business_id)
        self.install(db, owner)
        client = TestClient(app)
        posted = client.post("/api/v1/conversation/messages?business_id=" + str(business_id),
                             json={"content": "Hello team"})
        self.assertEqual(posted.status_code, 201, posted.text)
        self.assertEqual([item["sender_role"] for item in posted.json()], ["owner", "agent"])
        self.assertEqual(posted.json()[0]["content"], "Hello team")
        self.assertEqual(len(db.rows), 2)

        accountant = _context("accountant", business_id)
        self.install(db, accountant)
        history = client.get("/api/v1/conversation?business_id=" + str(business_id))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()["messages"]), 2)
        self.assertIn("LedgerRoom AI", [message["sender_name"] for message in history.json()["messages"]])

    def test_messages_are_isolated_by_business(self):
        db = _Session()
        business_a, business_b = uuid4(), uuid4()
        self.install(db, _context("owner", business_a))
        client = TestClient(app)
        client.post("/api/v1/conversation/messages?business_id=" + str(business_a), json={"content": "A-only"})
        db.rows.append(ConversationMessage(
            id=uuid4(), business_id=business_b, sender_user_id=uuid4(), sender_role="owner",
            sender_name="Other owner", content="B-only", attachments=[],
            created_at=datetime.now(timezone.utc),
        ))
        response = client.get("/api/v1/conversation?business_id=" + str(business_a))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["messages"]), 2)
        self.assertNotIn("B-only", [m["content"] for m in response.json()["messages"]])

    def test_accountant_can_chat_but_cannot_trigger_owner_only_reconciliation(self):
        db = _Session()
        context = _context("accountant", uuid4())
        self.install(db, context)
        with patch("backend.app.api.conversation.run_reconciliation") as reconcile:
            response = TestClient(app).post(
                "/api/v1/conversation/messages?business_id=" + str(context.business_id),
                json={"content": "/reconcile"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn("owner-only", response.json()[1]["content"])
        reconcile.assert_not_called()

    def test_owner_agent_command_calls_existing_reconciliation_service(self):
        db = _Session()
        context = _context("owner", uuid4())
        self.install(db, context)
        result = SimpleNamespace(
            total_invoices=2, matched=1, amount_mismatch=0, missing_payment=1,
            possible_duplicate=0, needs_review=0,
        )
        with patch("backend.app.api.conversation.run_reconciliation", return_value=result) as reconcile:
            response = TestClient(app).post(
                "/api/v1/conversation/messages?business_id=" + str(context.business_id),
                json={"content": "/reconcile"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn("finished for 2 invoices", response.json()[1]["content"])
        reconcile.assert_called_once_with(db, context.business_id)

    def test_invoice_attachment_must_belong_to_active_business(self):
        db = _Session()
        context = _context("owner", uuid4())
        self.install(db, context)
        response = TestClient(app).post(
            "/api/v1/conversation/messages?business_id=" + str(context.business_id),
            json={"content": "extract this", "attachments": [
                {"kind": "invoice", "filename": "invoice.png", "invoice_id": str(uuid4())},
            ]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(db.rows, [])

    def test_untrusted_attachment_fields_are_rejected(self):
        db = _Session()
        context = _context("owner", uuid4())
        self.install(db, context)
        response = TestClient(app).post(
            "/api/v1/conversation/messages?business_id=" + str(context.business_id),
            json={"content": "hello", "attachments": [{"kind": "invoice", "filename": "x.png", "invoice_id": str(uuid4()), "url": "https://example.invalid"}]},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
