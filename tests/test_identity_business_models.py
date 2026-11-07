"""Identity/business model and tenant constraint tests (no live database)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.orm import configure_mappers

from backend.app.db.base import Base
from backend.app.models import (
    AccountantProfile,
    AuthToken,
    BankLine,
    Business,
    BusinessInvitation,
    BusinessMembership,
    ChaseEvent,
    ClarificationReceipt,
    ClarificationRequest,
    ExpectedVendor,
    ExternalIdentity,
    Invoice,
    Match,
    MatchEvent,
    User,
)
from backend.app.cli import bootstrap_demo_owner


class IdentityBusinessModelTests(unittest.TestCase):
    def test_all_identity_models_are_registered_and_relationships_configure(self):
        configure_mappers()
        expected = {
            "users", "businesses", "business_memberships", "accountant_profiles",
            "business_invitations", "auth_tokens", "external_identities",
        }
        self.assertTrue(expected.issubset(Base.metadata.tables))
        self.assertEqual(BusinessMembership.user.property.mapper.class_, User)
        self.assertEqual(BusinessMembership.business.property.mapper.class_, Business)
        self.assertEqual(AccountantProfile.user.property.mapper.class_, User)
        self.assertEqual(BusinessInvitation.business.property.mapper.class_, Business)
        self.assertEqual(AuthToken.user.property.mapper.class_, User)
        self.assertEqual(ExternalIdentity.user.property.mapper.class_, User)

    def test_all_financial_rows_have_required_business_ownership(self):
        for model in (Invoice, BankLine, Match, MatchEvent, ExpectedVendor,
                      ClarificationRequest, ClarificationReceipt, ChaseEvent):
            with self.subTest(model=model.__name__):
                column = model.__table__.c.business_id
                self.assertFalse(column.nullable)
                self.assertIsNotNone(column.foreign_keys)
                self.assertTrue(any(fk.target_fullname == "businesses.id" for fk in column.foreign_keys))

    def test_composite_foreign_keys_include_tenant_id(self):
        expected = {
            Invoice: ("expected_vendor_id", "business_id"),
            Match: ("invoice_id", "business_id"),
            ClarificationRequest: ("bank_line_id", "business_id"),
            ClarificationReceipt: ("clarification_request_id", "business_id"),
            MatchEvent: ("match_id", "business_id"),
            ChaseEvent: ("invoice_id", "business_id"),
        }
        for model, columns in expected.items():
            with self.subTest(model=model.__name__):
                constraints = [constraint for constraint in model.__table__.constraints if isinstance(constraint, ForeignKeyConstraint)]
                self.assertTrue(any(tuple(column.name for column in constraint.columns) == columns for constraint in constraints))

    def test_composite_tenant_foreign_key_rejects_cross_business_reference(self):
        # Isolated SQLite fixture exercising the same composite FK shape; it
        # cannot connect to or alter the configured Supabase database.
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE businesses (id TEXT PRIMARY KEY);
            CREATE TABLE invoices (
                id TEXT NOT NULL, business_id TEXT NOT NULL,
                PRIMARY KEY (id), UNIQUE (id, business_id),
                FOREIGN KEY (business_id) REFERENCES businesses(id)
            );
            CREATE TABLE bank_lines (
                id TEXT NOT NULL, business_id TEXT NOT NULL,
                PRIMARY KEY (id), UNIQUE (id, business_id),
                FOREIGN KEY (business_id) REFERENCES businesses(id)
            );
            CREATE TABLE matches (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, bank_line_id TEXT,
                business_id TEXT NOT NULL,
                FOREIGN KEY (business_id) REFERENCES businesses(id),
                FOREIGN KEY (invoice_id, business_id) REFERENCES invoices(id, business_id),
                FOREIGN KEY (bank_line_id, business_id) REFERENCES bank_lines(id, business_id)
            );
        """)
        connection.executemany("INSERT INTO businesses(id) VALUES (?)", [("biz-a",), ("biz-b",)])
        connection.execute("INSERT INTO invoices(id,business_id) VALUES ('inv-a','biz-a')")
        connection.execute("INSERT INTO bank_lines(id,business_id) VALUES ('bank-b','biz-b')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO matches VALUES ('match-x','inv-a','bank-b','biz-a')")
        connection.close()

    def test_demo_bootstrap_requires_verified_user_and_is_one_time(self):
        email = "owner@example.test"
        business = SimpleNamespace(id=uuid4())
        unverified_user = SimpleNamespace(id=uuid4(), is_active=True, email_verified_at=None)
        factory = MagicMock()
        session = factory.begin.return_value.__enter__.return_value
        session.scalar.side_effect = [business, None, unverified_user]
        with patch("backend.app.cli.get_session_factory", return_value=factory), patch("builtins.input", return_value=email):
            with self.assertRaisesRegex(ValueError, "email-verified"):
                bootstrap_demo_owner(email)
        session.add.assert_not_called()

        verified_user = SimpleNamespace(id=uuid4(), is_active=True, email_verified_at=datetime.now(timezone.utc))
        session.scalar.side_effect = [business, None, verified_user, None]
        with patch("backend.app.cli.get_session_factory", return_value=factory), patch("builtins.input", return_value=email):
            bootstrap_demo_owner(email)
        membership = session.add.call_args.args[0]
        self.assertEqual(membership.business_id, business.id)
        self.assertEqual(membership.user_id, verified_user.id)
        self.assertEqual(membership.role, "owner")

        session.add.reset_mock()
        session.scalar.side_effect = [business, uuid4()]
        with patch("backend.app.cli.get_session_factory", return_value=factory), patch("builtins.input", return_value=email):
            with self.assertRaisesRegex(RuntimeError, "already has an owner"):
                bootstrap_demo_owner(email)
        session.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
