"""Tests for the verified Mattermost-to-LedgerRoom identity mapping."""

from __future__ import annotations

import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from backend.app.api.mattermost import finish_mattermost_identity_link, start_mattermost_identity_link
from backend.app.core.config import Settings
from backend.app.models import ExternalIdentity


class _Session:
    def __init__(self, scalar_results=()):
        self.results = deque(scalar_results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, _statement):
        return self.results.popleft() if self.results else None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MattermostOAuthMappingTests(unittest.TestCase):
    def test_link_url_uses_configured_callback_and_pkce(self):
        db = _Session()
        principal = SimpleNamespace(user=SimpleNamespace(id=uuid4()))
        settings = Settings(
            _env_file=None,
            APP_ENV="development",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
            MATTERMOST_OAUTH_CLIENT_ID="local-client-id",
            MATTERMOST_OAUTH_CLIENT_SECRET="local-client-secret",
            MATTERMOST_OAUTH_CALLBACK_URL="http://127.0.0.1:8010/api/v1/integrations/mattermost/callback",
        )
        with patch("backend.app.api.mattermost.get_settings", return_value=settings):
            result = start_mattermost_identity_link(principal=principal, db=db)

        parsed = urlsplit(result["authorization_url"])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["redirect_uri"], ["http://127.0.0.1:8010/api/v1/integrations/mattermost/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(len(params["state"][0]), 64)
        self.assertTrue(params["code_challenge"][0])
        self.assertEqual(len(db.added), 1)

    def test_callback_maps_verified_mattermost_identity_to_state_owner(self):
        ledgerroom_user_id = uuid4()
        state = SimpleNamespace(
            user_id=ledgerroom_user_id,
            code_verifier="pkce-verifier",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            consumed_at=None,
        )
        db = _Session((state, None, None))
        mattermost_id = "mmUser0123456789"

        with patch(
            "backend.app.api.mattermost.exchange_code",
            return_value=("http://127.0.0.1:45168", {"id": mattermost_id}),
        ) as exchange, patch(
            "backend.app.api.mattermost.sync_rooms_for_user", return_value=[],
        ) as sync, patch(
            "backend.app.api.mattermost.get_settings",
            return_value=Settings(_env_file=None, APP_ENV="development", AUTH_PUBLIC_BASE_URL="http://127.0.0.1:5173"),
        ):
            response = finish_mattermost_identity_link(
                code="oauth-code", state="opaque-state-value-0123456789012345", error=None, db=db,
            )

        exchange.assert_called_once_with(code="oauth-code", verifier="pkce-verifier")
        sync.assert_called_once_with(db, ledgerroom_user_id)
        identity = next(row for row in db.added if isinstance(row, ExternalIdentity))
        self.assertEqual(identity.user_id, ledgerroom_user_id)
        self.assertEqual(identity.provider, "mattermost")
        self.assertEqual(identity.workspace_id, "http://127.0.0.1:45168")
        self.assertEqual(identity.external_user_id, mattermost_id)
        self.assertIsNotNone(state.consumed_at)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://127.0.0.1:5173/?mattermost=linked")

    def test_callback_rejects_mattermost_identity_owned_by_another_account(self):
        ledgerroom_user_id = uuid4()
        state = SimpleNamespace(
            user_id=ledgerroom_user_id,
            code_verifier="pkce-verifier",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            consumed_at=None,
        )
        existing = SimpleNamespace(user_id=uuid4(), external_user_id="mmUser0123456789")
        db = _Session((state, existing))
        with patch(
            "backend.app.api.mattermost.exchange_code",
            return_value=("http://127.0.0.1:45168", {"id": existing.external_user_id}),
        ), patch("backend.app.api.mattermost.sync_rooms_for_user") as sync:
            with self.assertRaises(HTTPException) as caught:
                finish_mattermost_identity_link(
                    code="oauth-code", state="opaque-state-value-0123456789012345", error=None, db=db,
                )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(db.added, [])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
