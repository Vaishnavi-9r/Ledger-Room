"""Invitation and accountant-profile tests; no database or SMTP is contacted."""

from __future__ import annotations

import unittest
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db.session import get_db
from backend.app.api.accountants import _InvitationAccessLogFilter
from backend.app.dependencies.auth import AuthenticatedUser, get_current_auth, get_verified_auth
from backend.app.main import app
from backend.app.models import AccountantProfile, Business, BusinessInvitation, BusinessMembership, User
from backend.app.services.accountant_invitation_service import (
    InvitationError,
    accept_invitation,
    create_invitation,
)
from backend.app.services.auth_security import hash_opaque_token


NOW = datetime.now(timezone.utc)


def _principal(email="accountant@example.test", verified=True):
    user = SimpleNamespace(
        id=uuid4(), full_name="Asha Accountant", email=email,
        email_normalized=email.casefold(), email_verified_at=NOW if verified else None,
        is_active=True, updated_at=NOW,
    )
    return AuthenticatedUser(user=user, family_id=uuid4(), token_id="test")


def _business():
    return SimpleNamespace(
        id=uuid4(), business_name="Ledger Shop", is_demo=False,
    )


def _membership(business, user, role="owner"):
    return SimpleNamespace(
        id=uuid4(), business_id=business.id, user_id=user.id,
        role=role, status="active",
    )


class _Result:
    def __init__(self, row=None):
        self.row = row

    def first(self):
        return self.row


class _Session:
    def __init__(self, *, owner_row=None, invitation_row=None, membership=None, fail_commit=False):
        self.owner_row = owner_row
        self.invitation_row = invitation_row
        self.membership = membership
        self.fail_commit = fail_commit
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self.get_result = None

    def execute(self, _statement):
        return _Result(self.owner_row or self.invitation_row)

    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: [])

    def scalar(self, _statement):
        return self.membership

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commit_count += 1
        if self.fail_commit:
            raise SQLAlchemyError("simulated db failure")

    def rollback(self):
        self.rollback_count += 1
        self.added.clear()

    def get(self, _model, _key):
        return self.get_result


class AccountantInvitationTests(unittest.TestCase):
    def tearDown(self):
        for dependency in (get_db, get_current_auth, get_verified_auth):
            app.dependency_overrides.pop(dependency, None)

    def _client(self, session, principal=None, *, verified=True):
        app.dependency_overrides[get_db] = lambda: session
        if principal is not None:
            app.dependency_overrides[get_current_auth] = lambda: principal
            if verified:
                app.dependency_overrides[get_verified_auth] = lambda: principal
            else:
                app.dependency_overrides[get_verified_auth] = lambda: (_ for _ in ()).throw(
                    __import__("fastapi").HTTPException(status_code=403, detail="Email verification is required.")
                )
        return TestClient(app)

    def test_owner_only_and_cross_business_invitation_isolation(self):
        business, principal = _business(), _principal("owner@example.test")
        session = _Session(owner_row=(business, _membership(business, principal.user, "accountant")))
        client = self._client(session, principal)
        with patch("backend.app.api.accountants.get_email_provider", return_value=object()):
            response = client.post(
                f"/api/v1/businesses/{business.id}/invitations",
                json={"email": "new@example.test"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(session.added, [])

        session.owner_row = None
        list_response = client.get(f"/api/v1/businesses/{business.id}/invitations")
        self.assertEqual(list_response.status_code, 404)

    def test_unverified_owner_cannot_invite(self):
        client = self._client(_Session(), _principal(verified=False), verified=False)
        response = client.post(
            f"/api/v1/businesses/{uuid4()}/invitations", json={"email": "a@example.test"},
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_invitation_returns_only_the_authenticated_owner_share_link(self):
        business, principal = _business(), _principal("owner@example.test")
        membership = _membership(business, principal.user)
        session = _Session(owner_row=(business, membership))
        invitation = SimpleNamespace(
            id=uuid4(), business_id=business.id, email="invitee@example.test",
            role="accountant", status="pending", expires_at=NOW + timedelta(days=7),
            created_at=NOW, accepted_at=None, revoked_at=None,
        )
        provider = SimpleNamespace(send_accountant_invitation=Mock())
        client = self._client(session, principal)
        with (
            patch("backend.app.api.accountants.get_email_provider", return_value=provider),
            patch("backend.app.api.accountants.create_invitation", return_value=(invitation, "RAW_SECRET_TOKEN")),
        ):
            response = client.post(
                f"/api/v1/businesses/{business.id}/invitations",
                json={"email": "invitee@example.test"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("token_hash", response.text)
        self.assertEqual(
            response.json()["shareable_invitation_link"],
            "http://127.0.0.1:5173/accountant-invitations/accept?token=RAW_SECRET_TOKEN",
        )
        provider.send_accountant_invitation.assert_called_once_with(
            "invitee@example.test", "RAW_SECRET_TOKEN", business.business_name,
        )

    def test_local_invitation_link_is_returned_only_when_explicitly_enabled(self):
        from backend.app.core.config import Settings

        business, principal = _business(), _principal("owner@example.test")
        membership = _membership(business, principal.user)
        session = _Session(owner_row=(business, membership))
        invitation = SimpleNamespace(
            id=uuid4(), business_id=business.id, email="invitee@example.test",
            role="accountant", status="pending", expires_at=NOW + timedelta(days=7),
            created_at=NOW, accepted_at=None, revoked_at=None,
        )
        settings = Settings(
            _env_file=None, APP_ENV="development", AUTH_DEV_RETURN_INVITATION_LINK=True,
            AUTH_PUBLIC_BASE_URL="http://localhost:5173",
        )
        client = self._client(session, principal)
        with (
            patch("backend.app.api.accountants.get_settings", return_value=settings),
            patch("backend.app.api.accountants.get_email_provider", return_value=None),
            patch("backend.app.api.accountants.create_invitation", return_value=(invitation, "RAW_DEV_INVITATION")),
        ):
            response = client.post(
                f"/api/v1/businesses/{business.id}/invitations",
                json={"email": "invitee@example.test"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["email_submission"], "development_link_only")
        self.assertIn("RAW_DEV_INVITATION", response.json()["development_invitation_link"])

    def test_local_invitation_link_setting_is_rejected_in_production(self):
        from pydantic import ValidationError
        from backend.app.core.config import Settings

        with self.assertRaises(ValidationError):
            Settings(_env_file=None, APP_ENV="production", AUTH_DEV_RETURN_INVITATION_LINK=True)

    def test_invitation_token_is_hashed_and_expires_in_seven_days(self):
        business = _business()
        inviter = _membership(business, _principal("owner@example.test").user)
        session = _Session()
        invitation, raw_token = create_invitation(
            session, business=business, inviter_membership=inviter,
            email="Invitee@Example.test", email_normalized="invitee@example.test",
        )
        self.assertEqual(invitation.token_hash, hash_opaque_token(raw_token))
        self.assertNotEqual(invitation.token_hash, raw_token)
        self.assertEqual(invitation.email_normalized, "invitee@example.test")
        self.assertAlmostEqual((invitation.expires_at - invitation.created_at).total_seconds(), 7 * 86400, delta=2)
        self.assertEqual(session.commit_count, 1)

    def _invite(self, email="accountant@example.test", *, expires=None, status="pending"):
        business = _business()
        user = _principal(email).user
        row = SimpleNamespace(
            id=uuid4(), business_id=business.id, business=business,
            email=email, email_normalized=email.casefold(), status=status,
            expires_at=expires or NOW + timedelta(days=2), accepted_at=None,
            revoked_at=None,
        )
        return row, business, user

    def test_existing_and_new_verified_accounts_can_accept_once(self):
        for email in ("existing@example.test", "new@example.test"):
            with self.subTest(email=email):
                invitation, business, user = self._invite(email)
                session = _Session(invitation_row=(invitation, business))
                accepted, membership = accept_invitation(session, raw_token="R" * 48, user=user)
                self.assertEqual(accepted.status, "accepted")
                self.assertEqual(membership.role, "accountant")
                self.assertEqual(membership.status, "active")
                self.assertEqual(session.commit_count, 1)
                with self.assertRaises(InvitationError):
                    accept_invitation(session, raw_token="R" * 48, user=user)

    def test_expired_revoked_and_email_mismatch_invitations_are_rejected(self):
        invitation, business, user = self._invite(expires=NOW - timedelta(seconds=1))
        session = _Session(invitation_row=(invitation, business))
        with self.assertRaises(InvitationError) as caught:
            accept_invitation(session, raw_token="x" * 48, user=user)
        self.assertEqual(caught.exception.code, "expired")
        self.assertEqual(invitation.status, "expired")

        invitation, business, user = self._invite(status="revoked")
        with self.assertRaises(InvitationError) as caught:
            accept_invitation(_Session(invitation_row=(invitation, business)), raw_token="x" * 48, user=user)
        self.assertEqual(caught.exception.code, "inactive")

        invitation, business, user = self._invite()
        wrong_user = _principal("other@example.test").user
        with self.assertRaises(InvitationError) as caught:
            accept_invitation(_Session(invitation_row=(invitation, business)), raw_token="x" * 48, user=wrong_user)
        self.assertEqual(caught.exception.code, "email_mismatch")

    def test_duplicate_active_membership_is_not_created(self):
        invitation, business, user = self._invite()
        existing = SimpleNamespace(status="active", role="accountant")
        session = _Session(invitation_row=(invitation, business), membership=existing)
        with self.assertRaises(InvitationError) as caught:
            accept_invitation(session, raw_token="x" * 48, user=user)
        self.assertEqual(caught.exception.code, "membership_exists")
        self.assertEqual(session.added, [])
        self.assertEqual(session.commit_count, 0)

    def test_acceptance_rolls_back_membership_and_invitation_on_database_failure(self):
        invitation, business, user = self._invite()
        session = _Session(invitation_row=(invitation, business), fail_commit=True)
        with self.assertRaises(SQLAlchemyError):
            accept_invitation(session, raw_token="x" * 48, user=user)
        self.assertEqual(session.rollback_count, 1)
        self.assertEqual(session.added, [])

    def test_invitation_response_and_profile_routes_do_not_expose_other_user_data(self):
        own = _principal()
        session = _Session()
        client = self._client(session, own)
        response = client.get("/api/v1/accountants/me")
        self.assertEqual(response.status_code, 404)
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/accountants/me", paths)
        self.assertNotIn("/api/v1/accountants/{user_id}", paths)

    def test_access_log_filter_redacts_invitation_query_tokens(self):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 1, '%s "%s %s HTTP/%s" %d',
            ("127.0.0.1", "GET", "/api/v1/accountant-invitations/validate?token=SECRET_VALUE", "1.1", 200),
            None,
        )
        _InvitationAccessLogFilter().filter(record)
        rendered = record.getMessage()
        self.assertIn("token=[REDACTED]", rendered)
        self.assertNotIn("SECRET_VALUE", rendered)


if __name__ == "__main__":
    unittest.main()
