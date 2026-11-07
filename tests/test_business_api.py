"""Business profile API tests using isolated fake sessions (no Supabase)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.app.db.session import get_db
from backend.app.dependencies.auth import AuthenticatedUser, get_current_auth, get_verified_auth
from backend.app.main import app
from backend.app.models import Business, BusinessMembership, User
from backend.app.schemas.business import BusinessCreateRequest, gstin_checksum_valid
from backend.app.services.business_service import list_user_businesses


def _principal(*, verified=True):
    return AuthenticatedUser(
        user=SimpleNamespace(
            id=uuid4(), full_name="Owner", email="owner@example.test",
            email_verified_at=datetime.now(timezone.utc) if verified else None,
            is_active=True,
        ),
        family_id=uuid4(), token_id="test-jti",
    )


def _business(*, is_demo=False):
    return Business(
        id=uuid4(), business_name="Shop", proprietor_name="Owner", gstin=None,
        business_type="retail", business_email=None, business_phone=None,
        address_line_1="1 Main Road", address_line_2=None, city="Pune",
        state="Maharashtra", pin_code="411001", country="IN",
        gst_registration_status="unregistered", is_demo=is_demo,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


class _Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def first(self):
        return self.row

    def all(self):
        return self.rows


class _Session:
    def __init__(self, *, scalar=None, scalar_rows=(), execute_rows=(), fail_membership=False, fail_commit=False):
        self.scalar_result = scalar
        self.scalar_rows = list(scalar_rows)
        self.execute_rows = list(execute_rows)
        self.added = []
        self.committed = []
        self.commit_count = 0
        self.rollback_count = 0
        self.fail_membership = fail_membership
        self.fail_commit = fail_commit
        self.statements = []

    def scalar(self, _statement):
        return self.scalar_result

    def scalars(self, _statement):
        self.statements.append(_statement)
        return SimpleNamespace(all=lambda: list(self.scalar_rows))

    def execute(self, statement):
        self.statements.append(statement)
        row = self.execute_rows.pop(0) if self.execute_rows else None
        return _Result(row=row)

    def add(self, row):
        if self.fail_membership and isinstance(row, BusinessMembership):
            raise IntegrityError("membership insert", {}, Exception("constraint"))
        self.added.append(row)

    def commit(self):
        self.commit_count += 1
        if self.fail_commit:
            raise IntegrityError("commit", {}, Exception("constraint"))
        self.committed.extend(self.added)

    def rollback(self):
        self.rollback_count += 1
        self.added.clear()


class BusinessApiTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_auth, None)
        app.dependency_overrides.pop(get_verified_auth, None)

    def _install(self, session, principal=None, *, verified_dependency=True):
        app.dependency_overrides[get_db] = lambda: session
        if principal is not None:
            app.dependency_overrides[get_current_auth] = lambda: principal
            if verified_dependency:
                app.dependency_overrides[get_verified_auth] = lambda: principal

    @staticmethod
    def _payload(**overrides):
        data = {
            "business_name": "Ledger Shop", "proprietor_name": "Asha Rao",
            "business_type": "retail", "business_email": None, "business_phone": None,
            "address_line_1": "1 Market Road", "address_line_2": None,
            "city": "Pune", "state": "Maharashtra", "pin_code": "411001",
        }
        data.update(overrides)
        return data

    def test_unauthenticated_and_unverified_users_cannot_register(self):
        session = _Session()
        self._install(session)
        client = TestClient(app)
        response = client.post("/api/v1/businesses/", json=self._payload())
        self.assertEqual(response.status_code, 401)

        self.tearDown()
        self._install(session, _principal(verified=False), verified_dependency=False)
        response = TestClient(app).post("/api/v1/businesses/", json=self._payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.added, [])

    def test_business_creation_creates_owner_membership_atomically(self):
        session = _Session()
        principal = _principal()
        self._install(session, principal)
        response = TestClient(app).post("/api/v1/businesses/", json=self._payload())
        self.assertEqual(response.status_code, 201, response.text)
        business = next(row for row in session.committed if isinstance(row, Business))
        membership = next(row for row in session.committed if isinstance(row, BusinessMembership))
        self.assertFalse(business.is_demo)
        self.assertEqual(membership.business_id, business.id)
        self.assertEqual(membership.user_id, principal.user.id)
        self.assertEqual(membership.role, "owner")
        self.assertEqual(membership.status, "active")
        self.assertEqual(session.commit_count, 1)
        self.assertIsNone(business.business_email)
        self.assertIsNone(business.business_phone)
        self.assertNotIn("role", self._payload())

    def test_membership_insert_failure_rolls_back_business(self):
        session = _Session(fail_membership=True)
        self._install(session, _principal())
        response = TestClient(app).post("/api/v1/businesses/", json=self._payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(session.rollback_count, 1)
        self.assertEqual(session.committed, [])

    def test_gstin_format_checksum_and_duplicate_database_constraint(self):
        valid_gstin = "27AAACR5055K1Z7"
        self.assertTrue(gstin_checksum_valid(valid_gstin))
        self._install(_Session(), _principal())
        invalid = TestClient(app).post("/api/v1/businesses/", json=self._payload(gstin="27AAACR5055K1Z8"))
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("checksum", invalid.text.lower())

        session = _Session(fail_commit=True)
        self._install(session, _principal())
        response = TestClient(app).post("/api/v1/businesses/", json=self._payload(
            gstin=valid_gstin, gst_registration_status="registered",
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(session.rollback_count, 1)

    def test_listing_query_is_limited_to_active_membership_user(self):
        session = _Session(scalar_rows=[_business()])
        user_id = uuid4()
        result = list_user_businesses(session, user_id)
        self.assertEqual(len(result), 1)
        sql = str(session.statements[0])
        self.assertIn("business_memberships.user_id", sql)
        self.assertIn("business_memberships.status", sql)

    def test_cross_business_and_inactive_membership_return_404(self):
        session = _Session(execute_rows=[None, None])
        self._install(session, _principal())
        client = TestClient(app)
        self.assertEqual(client.get(f"/api/v1/businesses/{uuid4()}").status_code, 404)
        self.assertEqual(client.get(f"/api/v1/businesses/{uuid4()}/members").status_code, 404)

    def test_accountant_can_view_but_cannot_update_owner_profile(self):
        business = _business()
        membership = SimpleNamespace(role="accountant")
        session = _Session(execute_rows=[(business, membership)])
        self._install(session, _principal())
        response = TestClient(app).patch(
            f"/api/v1/businesses/{business.id}", json={"business_name": "Changed"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.commit_count, 0)

    def test_ordinary_owner_cannot_modify_demo_business(self):
        business = _business(is_demo=True)
        membership = SimpleNamespace(role="owner")
        session = _Session(execute_rows=[(business, membership)])
        self._install(session, _principal())
        response = TestClient(app).patch(
            f"/api/v1/businesses/{business.id}", json={"business_name": "Claimed"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(session.commit_count, 0)


if __name__ == "__main__":
    unittest.main()
