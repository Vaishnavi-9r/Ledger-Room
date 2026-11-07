"""Unit coverage for private Mattermost finance-channel provisioning."""

from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import call, patch
from uuid import uuid4

from backend.app.core.config import Settings
from backend.app.models import BusinessRoom
from backend.app.services.mattermost_service import MattermostError, sync_business_room


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, business, memberships, identity_results):
        self.business = business
        self.memberships = memberships
        self.identity_results = deque(identity_results)
        self.commits = 0

    def get(self, model, _key):
        return self.business if model.__name__ == "Business" else None

    def scalars(self, _statement):
        return _Scalars(self.memberships)

    def scalar(self, _statement):
        return self.identity_results.popleft() if self.identity_results else None

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        return None


class MattermostRoomProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.business_id = uuid4()
        self.business = SimpleNamespace(business_name="Ledger Demo")
        self.owner = SimpleNamespace(user_id=uuid4(), role="owner", status="active")
        self.accountant = SimpleNamespace(user_id=uuid4(), role="accountant", status="active")
        self.owner_identity = SimpleNamespace(external_user_id="mattermost-owner")
        self.accountant_identity = SimpleNamespace(external_user_id="mattermost-accountant")
        self.settings = Settings(
            _env_file=None,
            APP_ENV="development",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
            MATTERMOST_TEAM_ID="local-team",
            MATTERMOST_BOT_TOKEN="test-service-token",
            MATTERMOST_SWITCH_BOT_USER_ID="switch-agent-user",
        )

    def test_creates_private_channel_once_and_syncs_linked_members(self):
        room = BusinessRoom(id=uuid4(), business_id=self.business_id, mattermost_status="pending")
        db = _Session(
            self.business,
            [self.owner, self.accountant],
            [self.owner_identity, self.accountant_identity, self.owner_identity] * 2,
        )
        with patch(
            "backend.app.services.mattermost_service._request",
            return_value={"id": "private-channel-id"},
        ) as request, patch(
            "backend.app.services.mattermost_service._ensure_channel_member",
        ) as ensure_member:
            sync_business_room(db, room, settings=self.settings)
            sync_business_room(db, room, settings=self.settings)

        self.assertEqual(room.mattermost_status, "ready")
        self.assertEqual(room.mattermost_channel_id, "private-channel-id")
        self.assertEqual(room.mattermost_team_id, "local-team")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:3], ("POST", "http://127.0.0.1:45168", "channels"))
        self.assertEqual(request.call_args.kwargs["payload"]["type"], "P")
        ensure_member.assert_has_calls([
            call("http://127.0.0.1:45168", "test-service-token", "private-channel-id", "mattermost-accountant"),
            call("http://127.0.0.1:45168", "test-service-token", "private-channel-id", "mattermost-owner"),
            call("http://127.0.0.1:45168", "test-service-token", "private-channel-id", "switch-agent-user"),
        ] * 2)

    def test_does_not_create_channel_before_owner_identity_is_linked(self):
        room = BusinessRoom(id=uuid4(), business_id=self.business_id, mattermost_status="pending")
        db = _Session(self.business, [self.owner], [None, None])
        with patch("backend.app.services.mattermost_service._request") as request:
            sync_business_room(db, room, settings=self.settings)

        self.assertEqual(room.mattermost_status, "pending_identity_link")
        self.assertIsNone(room.mattermost_channel_id)
        request.assert_not_called()

    def test_member_sync_failure_keeps_channel_mapping_for_idempotent_retry(self):
        room = BusinessRoom(id=uuid4(), business_id=self.business_id, mattermost_status="pending")
        db = _Session(
            self.business,
            [self.owner, self.accountant],
            [self.owner_identity, self.accountant_identity, self.owner_identity] * 2,
        )
        with patch(
            "backend.app.services.mattermost_service._request",
            return_value={"id": "private-channel-id"},
        ) as request, patch(
            "backend.app.services.mattermost_service._ensure_channel_member",
            side_effect=[MattermostError("mattermost_unavailable", "unavailable"), None, None, None],
        ) as ensure_member:
            failed = sync_business_room(db, room, settings=self.settings)
            self.assertEqual(failed.mattermost_status, "error")
            self.assertEqual(failed.mattermost_channel_id, "private-channel-id")

            retried = sync_business_room(db, room, settings=self.settings)

        self.assertEqual(retried.mattermost_status, "ready")
        self.assertEqual(retried.mattermost_channel_id, "private-channel-id")
        request.assert_called_once()
        self.assertEqual(ensure_member.call_count, 4)


if __name__ == "__main__":
    unittest.main()
