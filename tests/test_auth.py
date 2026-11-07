"""Authentication flow tests with fake sessions; never use live Supabase."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.db.session import get_db
from backend.app.dependencies.auth import AuthenticatedUser, get_current_auth, get_verified_auth
from backend.app.main import app
from backend.app.models import AuthToken, BusinessMembership, User
from backend.app.services import auth_service
from backend.app.services.auth_security import (
    AuthConfigurationError,
    ProcessRateLimiter,
    create_access_token,
    decode_access_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from backend.app.services.auth_service import (
    DuplicateAccountError,
    InvalidCredentialsError,
    authenticate_user,
    register_user,
    revoke_refresh_family,
    rotate_refresh_token,
    verify_email_token,
)
from jwt import InvalidTokenError


NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings(
    _env_file=None,
    AUTH_JWT_SIGNING_KEY=SecretStr("k" * 48),
    AUTH_ACCESS_TOKEN_TTL_SECONDS=600,
    AUTH_REFRESH_TOKEN_TTL_DAYS=14,
    AUTH_EMAIL_VERIFICATION_TTL_HOURS=24,
)


class _FakeSession:
    def __init__(self, scalar_values=(), membership_rows=()):
        self.scalar_values = list(scalar_values)
        self.membership_rows = list(membership_rows)
        self.added = []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, _statement):
        return self.scalar_values.pop(0) if self.scalar_values else None

    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: list(self.membership_rows))

    def add(self, row):
        self.added.append(row)

    def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _user(*, verified=True, password_hash=None):
    return SimpleNamespace(
        id=uuid4(), full_name="Ledger User", email="Owner@Example.test",
        email_normalized="owner@example.test", mobile_number="+919876543210",
        password_hash=password_hash or hash_password("Secure-Passphrase-2026!"),
        email_verified_at=NOW if verified else None, is_active=True,
    )


class AuthServiceTests(unittest.TestCase):
    def test_email_verification_can_be_disabled_only_in_local_environments(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, APP_ENV="production", AUTH_REQUIRE_EMAIL_VERIFICATION=False)
        local = Settings(_env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False)
        self.assertFalse(local.AUTH_REQUIRE_EMAIL_VERIFICATION)
        self.assertTrue(Settings(_env_file=None, APP_ENV="production").AUTH_REQUIRE_EMAIL_VERIFICATION)

    def test_password_hashing_uses_scrypt_and_never_stores_plain_text(self):
        password = "Secure-Passphrase-2026!"
        encoded = hash_password(password)
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertNotIn(password, encoded)
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password("wrong-password", encoded))

    def test_registration_creates_unverified_user_and_stores_only_token_hash(self):
        db = _FakeSession([None])
        with patch.object(auth_service, "_now", return_value=NOW):
            user, raw = register_user(
                db, full_name="Owner", email="Owner@Example.test",
                normalized_email="owner@example.test", mobile_number="+919876543210",
                password="Secure-Passphrase-2026!", settings=SETTINGS,
            )
        self.assertIsNone(user.email_verified_at)
        self.assertNotEqual(user.password_hash, "Secure-Passphrase-2026!")
        stored_auth_token = next(row for row in db.added if isinstance(row, AuthToken))
        self.assertEqual(stored_auth_token.token_hash, hash_opaque_token(raw))
        self.assertNotEqual(stored_auth_token.token_hash, raw)
        self.assertEqual(db.commits, 1)

    def test_duplicate_email_registration_is_rejected(self):
        with self.assertRaises(DuplicateAccountError):
            register_user(
                _FakeSession([uuid4()]), full_name="Owner", email="owner@example.test",
                normalized_email="owner@example.test", mobile_number="+919876543210",
                password="Secure-Passphrase-2026!", settings=SETTINGS,
            )

    def test_invalid_and_unverified_login_are_rejected_generically(self):
        with patch.object(auth_service, "get_settings", return_value=SETTINGS):
            with self.assertRaisesRegex(InvalidCredentialsError, "Invalid email or password"):
                authenticate_user(_FakeSession([None]), normalized_email="nobody@example.test", password="wrong")
            unverified = _user(verified=False)
            with self.assertRaisesRegex(InvalidCredentialsError, "Invalid email or password"):
                authenticate_user(_FakeSession([unverified]), normalized_email="owner@example.test", password="Secure-Passphrase-2026!")

    def test_unverified_user_can_password_login_when_local_setting_disables_requirement(self):
        user = _user(verified=False)
        local = Settings(
            _env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False,
            AUTH_JWT_SIGNING_KEY=SecretStr("k" * 48), AUTH_ACCESS_TOKEN_TTL_SECONDS=600,
        )
        with patch.object(auth_service, "get_settings", return_value=local):
            result = authenticate_user(
                _FakeSession([user]), normalized_email=user.email_normalized,
                password="Secure-Passphrase-2026!",
            )
        self.assertIs(result[0], user)
        self.assertTrue(result[1])
        self.assertTrue(result[2])
        self.assertIsNone(user.email_verified_at)

    def test_incorrect_password_for_existing_user_is_rejected(self):
        with patch.object(auth_service, "get_settings", return_value=SETTINGS):
            with self.assertRaisesRegex(InvalidCredentialsError, "Invalid email or password"):
                authenticate_user(
                    _FakeSession([_user()]), normalized_email="owner@example.test",
                    password="Incorrect-Passphrase-2026!",
                )

    def test_email_verification_expiry_and_single_use(self):
        token = AuthToken(
            id=uuid4(), user_id=uuid4(), token_hash=hash_opaque_token("v" * 48),
            purpose="email_verification", family_id=None, expires_at=NOW - timedelta(seconds=1),
        )
        user = _user()
        with patch.object(auth_service, "_now", return_value=NOW):
            with self.assertRaises(InvalidCredentialsError):
                verify_email_token(_FakeSession([token, user, token]), raw_token="v" * 48)
            token.expires_at = NOW + timedelta(hours=1)
            db = _FakeSession([token, user, token])
            verified = verify_email_token(db, raw_token="v" * 48)
            self.assertIs(verified, user)
            self.assertIsNotNone(user.email_verified_at)
            self.assertIsNotNone(token.consumed_at)
            with self.assertRaises(InvalidCredentialsError):
                verify_email_token(_FakeSession([token, user, token]), raw_token="v" * 48)

    def test_refresh_rotation_and_replay_revoke_entire_family(self):
        family_id = uuid4()
        user = _user()
        original = AuthToken(
            id=uuid4(), user_id=user.id, token_hash=hash_opaque_token("r" * 48),
            purpose="refresh", family_id=family_id, expires_at=NOW + timedelta(days=10),
            consumed_at=None, revoked_at=None,
        )
        with patch.object(auth_service, "get_settings", return_value=SETTINGS), patch.object(auth_service, "_now", return_value=NOW):
            db = _FakeSession([original, user])
            _user_result, access, replacement, expires_in, refresh_days = rotate_refresh_token(db, raw_token="r" * 48)
            self.assertTrue(access)
            self.assertNotEqual(replacement, "r" * 48)
            self.assertEqual(expires_in, 600)
            self.assertEqual(refresh_days, 14)
            self.assertIsNotNone(original.consumed_at)
            next_record = next(row for row in db.added if isinstance(row, AuthToken))
            self.assertEqual(next_record.family_id, family_id)
            self.assertEqual(next_record.token_hash, hash_opaque_token(replacement))
            self.assertNotEqual(next_record.token_hash, replacement)
            replay_db = _FakeSession([original])
            with self.assertRaises(InvalidCredentialsError):
                rotate_refresh_token(replay_db, raw_token="r" * 48)
            self.assertTrue(replay_db.executed)
            self.assertEqual(replay_db.commits, 1)

    def test_unverified_refresh_rotation_obeys_local_setting_without_marking_verified(self):
        family_id = uuid4()
        user = _user(verified=False)
        token = AuthToken(
            id=uuid4(), user_id=user.id, token_hash=hash_opaque_token("r" * 48),
            purpose="refresh", family_id=family_id, expires_at=NOW + timedelta(days=10),
        )
        local = Settings(
            _env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False,
            AUTH_JWT_SIGNING_KEY=SecretStr("k" * 48), AUTH_ACCESS_TOKEN_TTL_SECONDS=600,
        )
        with patch.object(auth_service, "get_settings", return_value=local), patch.object(auth_service, "_now", return_value=NOW):
            refreshed = rotate_refresh_token(_FakeSession([token, user]), raw_token="r" * 48)
        self.assertIs(refreshed[0], user)
        self.assertTrue(refreshed[1])
        self.assertNotEqual(refreshed[2], "r" * 48)
        self.assertIsNone(user.email_verified_at)

    def test_logout_revokes_refresh_family(self):
        family_id = uuid4()
        db = _FakeSession()
        revoke_refresh_family(db, family_id=family_id)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.executed), 1)

    def test_jwt_expiration_and_required_signing_secret(self):
        user_id, family_id = uuid4(), uuid4()
        token, _ = create_access_token(
            user_id=user_id, family_id=family_id, settings=SETTINGS,
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        with self.assertRaises(InvalidTokenError):
            decode_access_token(token, SETTINGS)
        missing = Settings(_env_file=None, AUTH_JWT_SIGNING_KEY=None)
        with self.assertRaises(AuthConfigurationError):
            create_access_token(user_id=user_id, family_id=family_id, settings=missing)


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        from backend.app.services.auth_security import auth_rate_limiter
        auth_rate_limiter.clear()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_auth, None)
        from backend.app.services.auth_security import auth_rate_limiter
        auth_rate_limiter.clear()

    @staticmethod
    def _registration_payload(email="Owner@Example.test"):
        return {
            "full_name": "Owner", "email": email, "mobile_number": "+919876543210",
            "password": "Secure-Passphrase-2026!", "password_confirmation": "Secure-Passphrase-2026!",
        }

    def test_me_rejects_unauthorized_request(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        response = TestClient(app).get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authentication required.")

    def test_me_returns_memberships_without_password_or_token_fields(self):
        user = _user()
        membership = SimpleNamespace(business_id=uuid4(), role="owner")
        app.dependency_overrides[get_db] = lambda: _FakeSession(membership_rows=[membership])
        app.dependency_overrides[get_current_auth] = lambda: AuthenticatedUser(user, uuid4(), "jti")
        response = TestClient(app).get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["memberships"][0]["role"], "owner")
        self.assertTrue(body["email_verified"])
        self.assertNotIn("password_hash", body)
        self.assertNotIn("access_token", body)
        self.assertNotIn("refresh_token", body)

    def test_verified_auth_gate_obeys_configuration_without_marking_user_verified(self):
        user = _user(verified=False)
        principal = AuthenticatedUser(user, uuid4(), "jti")
        local = Settings(_env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False)
        with patch("backend.app.dependencies.auth.get_settings", return_value=local):
            self.assertIs(get_verified_auth(principal), principal)
        production = Settings(_env_file=None, APP_ENV="production", AUTH_REQUIRE_EMAIL_VERIFICATION=True)
        with patch("backend.app.dependencies.auth.get_settings", return_value=production):
            with self.assertRaises(HTTPException) as raised:
                get_verified_auth(principal)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNone(user.email_verified_at)

    def test_validation_errors_do_not_echo_passwords(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        response = TestClient(app).post("/api/v1/auth/register", json={
            "full_name": "Owner", "email": "owner@example.test", "mobile_number": "+919876543210",
            "password": "Secret-Password-2026!", "password_confirmation": "Different-Secret-2026!",
        })
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("Secret-Password-2026!", response.text)
        self.assertNotIn("Different-Secret-2026!", response.text)

    def test_registration_endpoint_returns_no_secret_by_default(self):
        session = _FakeSession([None])
        app.dependency_overrides[get_db] = lambda: session
        settings = Settings(_env_file=None, APP_ENV="development", AUTH_DEV_RETURN_VERIFICATION_LINK=False)
        payload = {
            "full_name": "Owner", "email": "Owner@Example.test", "mobile_number": "+919876543210",
            "password": "Secure-Passphrase-2026!", "password_confirmation": "Secure-Passphrase-2026!",
        }
        with patch("backend.app.api.auth.get_settings", return_value=settings):
            response = TestClient(app).post("/api/v1/auth/register", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("token", response.text.casefold())
        self.assertNotIn("password", response.text.casefold())
        user = next(row for row in session.added if isinstance(row, User))
        self.assertIsNone(user.email_verified_at)

    def test_registration_reports_verification_not_required_in_local_mode(self):
        session = _FakeSession([None])
        app.dependency_overrides[get_db] = lambda: session
        settings = Settings(
            _env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False,
            AUTH_DEV_RETURN_VERIFICATION_LINK=True, AUTH_PUBLIC_BASE_URL="http://localhost:5173",
        )
        with (
            patch("backend.app.api.auth.get_settings", return_value=settings),
            patch("backend.app.api.auth.get_email_provider") as provider,
        ):
            response = TestClient(app, client=("127.0.0.1", 43210)).post(
                "/api/v1/auth/register", json=self._registration_payload(),
            )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["email_verification_required"])
        self.assertEqual(response.json()["email_delivery_status"], "not_required")
        self.assertIsNone(response.json()["development_verification_link"])
        self.assertIn("sign in now", response.json()["message"])
        provider.assert_not_called()
        stored_user = next(row for row in session.added if isinstance(row, User))
        self.assertIsNone(stored_user.email_verified_at)

    def test_registration_reports_delivery_failure_without_leaking_token(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        settings = Settings(_env_file=None, APP_ENV="production", AUTH_DEV_RETURN_VERIFICATION_LINK=True)
        with (
            patch("backend.app.api.auth.get_settings", return_value=settings),
            patch("backend.app.api.auth.register_user", return_value=(_user(), "v" * 48)),
            patch("backend.app.api.auth.get_email_provider", return_value=None),
        ):
            response = TestClient(app, client=("127.0.0.1", 43210)).post(
                "/api/v1/auth/register", json=self._registration_payload(),
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["email_delivery_status"], "unavailable")
        self.assertIsNone(response.json()["development_verification_link"])
        self.assertNotIn("v" * 48, response.text)
        self.assertIn("unavailable", response.json()["message"])

    def test_duplicate_registration_returns_conflict(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        with (
            patch("backend.app.api.auth.get_settings", return_value=SETTINGS),
            patch("backend.app.api.auth.register_user", side_effect=DuplicateAccountError),
        ):
            response = TestClient(app).post("/api/v1/auth/register", json=self._registration_payload())
        self.assertEqual(response.status_code, 409)
        self.assertIn("already registered", response.json()["detail"])

    def test_verification_success_invalid_expired_and_reuse_statuses(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        token_payload = {"token": "verification-token-" + "v" * 48}
        with patch("backend.app.api.auth.verify_email_token", return_value=_user()):
            response = TestClient(app).post("/api/v1/auth/verify-email", json=token_payload)
        self.assertEqual(response.status_code, 200)
        for reason in ("invalid", "expired", "already used"):
            with self.subTest(reason=reason), patch(
                "backend.app.api.auth.verify_email_token", side_effect=InvalidCredentialsError("The token is invalid or expired."),
            ):
                response = TestClient(app).post("/api/v1/auth/verify-email", json=token_payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"], "The token is invalid or expired.")

    def test_login_rejects_unverified_and_returns_token_contract_for_verified_user(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        payload = {"email": "owner@example.test", "password": "Secure-Passphrase-2026!"}
        with patch("backend.app.api.auth.authenticate_user", side_effect=InvalidCredentialsError("Invalid email or password.")):
            response = TestClient(app).post("/api/v1/auth/login", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid email or password.")
        with (
            patch("backend.app.api.auth.get_settings", return_value=SETTINGS),
            patch("backend.app.api.auth.authenticate_user", return_value=(_user(), "access-value", "refresh-value", 600, 14)),
        ):
            response = TestClient(app).post("/api/v1/auth/login", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "token_type": "bearer", "access_token": "access-value",
            "refresh_token": "refresh-value", "expires_in": 600,
        })

    def test_refresh_route_returns_rotated_token_response(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        payload = {"refresh_token": "refresh-token-" + "r" * 48}
        with (
            patch("backend.app.api.auth.get_settings", return_value=SETTINGS),
            patch("backend.app.api.auth.rotate_refresh_token", return_value=(_user(), "new-access", "new-refresh", 600, 14)),
        ):
            response = TestClient(app).post("/api/v1/auth/refresh", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_token"], "new-access")
        self.assertEqual(response.json()["refresh_token"], "new-refresh")

    def test_valid_access_bearer_authenticates_me_and_business_list(self):
        user = _user(verified=False)
        family = uuid4()
        session = _FakeSession([user, uuid4(), user, uuid4()])
        app.dependency_overrides[get_db] = lambda: session
        access, _ = create_access_token(user_id=user.id, family_id=family, settings=SETTINGS)
        local = Settings(_env_file=None, APP_ENV="development", AUTH_REQUIRE_EMAIL_VERIFICATION=False, AUTH_JWT_SIGNING_KEY=SecretStr("k" * 48))
        with (
            patch("backend.app.dependencies.auth.get_settings", return_value=local),
            patch("backend.app.services.auth_security.get_settings", return_value=local),
        ):
            client = TestClient(app)
            headers = {"Authorization": f"Bearer {access}"}
            me = client.get("/api/v1/auth/me", headers=headers)
            businesses = client.get("/api/v1/businesses/", headers=headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], user.email)
        self.assertFalse(me.json()["email_verified"])
        self.assertEqual(businesses.status_code, 200)
        self.assertEqual(businesses.json(), {"businesses": []})

    def test_logout_revokes_family_and_rejects_old_access_token(self):
        user = _user()
        family = uuid4()
        session = _FakeSession([user, uuid4(), user, None])
        app.dependency_overrides[get_db] = lambda: session
        access, _ = create_access_token(user_id=user.id, family_id=family, settings=SETTINGS)
        with (
            patch("backend.app.services.auth_security.get_settings", return_value=SETTINGS),
            patch("backend.app.api.auth.revoke_refresh_family") as revoke,
        ):
            client = TestClient(app)
            headers = {"Authorization": f"Bearer {access}"}
            response = client.post("/api/v1/auth/logout", headers=headers)
            after_logout = client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(response.status_code, 200)
        revoke.assert_called_once_with(session, family_id=family)
        self.assertEqual(after_logout.status_code, 401)

    def test_development_verification_link_requires_explicit_loopback_opt_in(self):
        session = _FakeSession([None])
        app.dependency_overrides[get_db] = lambda: session
        settings = Settings(
            _env_file=None, APP_ENV="development", AUTH_DEV_RETURN_VERIFICATION_LINK=True,
            AUTH_PUBLIC_BASE_URL="http://localhost:3000",
        )
        payload = {
            "full_name": "Owner", "email": "Owner@Example.test", "mobile_number": "+919876543210",
            "password": "Secure-Passphrase-2026!", "password_confirmation": "Secure-Passphrase-2026!",
        }
        with patch("backend.app.api.auth.get_settings", return_value=settings):
            response = TestClient(app, client=("127.0.0.1", 43210)).post("/api/v1/auth/register", json=payload)
        self.assertEqual(response.status_code, 202)
        link = response.json()["development_verification_link"]
        self.assertTrue(link.startswith("http://localhost:3000/verify-email?token="))
        self.assertNotIn(link.split("token=", 1)[1], [row.token_hash for row in session.added if isinstance(row, AuthToken)])

    def test_configured_smtp_submission_is_reported_without_returning_a_token(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        provider = SimpleNamespace(send_verification=Mock())
        settings = Settings(_env_file=None, APP_ENV="production", AUTH_PUBLIC_BASE_URL="https://ledgerroom.example")
        with (
            patch("backend.app.api.auth.get_settings", return_value=settings),
            patch("backend.app.api.auth.register_user", return_value=(_user(), "v" * 48)),
            patch("backend.app.api.auth.get_email_provider", return_value=provider),
        ):
            response = TestClient(app).post("/api/v1/auth/register", json=self._registration_payload())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["email_delivery_status"], "submitted")
        self.assertIsNone(response.json()["development_verification_link"])
        self.assertNotIn("v" * 48, response.text)
        provider.send_verification.assert_called_once_with("Owner@Example.test", "v" * 48)

    def test_resend_uses_generic_response_and_development_link_is_loopback_only(self):
        app.dependency_overrides[get_db] = lambda: _FakeSession()
        settings = Settings(
            _env_file=None, APP_ENV="development", AUTH_DEV_RETURN_VERIFICATION_LINK=True,
            AUTH_PUBLIC_BASE_URL="http://localhost:5173",
        )
        payload = {"email": "owner@example.test"}
        with (
            patch("backend.app.api.auth.get_settings", return_value=settings),
            patch("backend.app.api.auth.create_resend_token", return_value=("owner@example.test", "r" * 48)),
            patch("backend.app.api.auth.get_email_provider", return_value=None),
        ):
            local = TestClient(app, client=("127.0.0.1", 43210)).post("/api/v1/auth/resend-verification", json=payload)
            remote = TestClient(app, client=("203.0.113.20", 43210)).post("/api/v1/auth/resend-verification", json=payload)
        self.assertEqual(local.status_code, 202)
        self.assertIn("token=", local.json()["development_verification_link"])
        self.assertEqual(remote.status_code, 202)
        self.assertIsNone(remote.json()["development_verification_link"])
        self.assertEqual(local.json()["message"], remote.json()["message"])

    def test_rate_limiter_enforces_window_limit(self):
        limiter = ProcessRateLimiter()
        self.assertEqual(limiter.check("login:test", limit=1, window_seconds=60), 0)
        self.assertGreaterEqual(limiter.check("login:test", limit=1, window_seconds=60), 1)


if __name__ == "__main__":
    unittest.main()
