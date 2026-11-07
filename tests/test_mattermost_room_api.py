"""Owner-only API tests for canonical LedgerRoom Mattermost room provisioning."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException

from backend.app.api.mattermost import create_or_sync_business_room
from backend.app.dependencies.auth import AuthenticatedUser
from backend.app.models import BusinessRoom


class _Session:
    def __init__(self, scalar_results, business):
        self.scalar_results = list(scalar_results)
        self.business = business
        self.added = []
        self.commits = 0

    def scalar(self, _statement):
        return self.scalar_results.pop(0) if self.scalar_results else None

    def get(self, model, _key):
        return self.business if model.__name__ == "Business" else None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, row):
        row.created_at = row.created_at or datetime.now(timezone.utc)
        row.updated_at = row.updated_at or row.created_at

    def rollback(self):
        pass


class MattermostRoomApiTests(unittest.TestCase):
    def setUp(self):
        self.business_id = uuid4()
        self.user_id = uuid4()
        self.business = SimpleNamespace(is_demo=False)
        self.owner_membership = SimpleNamespace(role="owner", status="active")
        self.principal = AuthenticatedUser(
            user=SimpleNamespace(id=self.user_id), family_id=uuid4(), token_id="test-session",
        )

    def test_active_owner_creates_and_persists_canonical_private_channel_mapping(self):
        db = _Session([self.owner_membership, None], self.business)
        def provision(_db, current_room):
            current_room.mattermost_server = "http://127.0.0.1:45168"
            current_room.mattermost_team_id = "team-id"
            current_room.mattermost_channel_id = "private-channel-id"
            current_room.mattermost_channel_name = "lr-test-room"
            current_room.mattermost_status = "ready"
            return current_room

        with patch("backend.app.api.mattermost.sync_business_room", side_effect=provision) as sync:
            response = create_or_sync_business_room(
                str(self.business_id), principal=self.principal, db=db,
            )

        self.assertEqual(response.mattermost_status, "ready")
        self.assertEqual(response.mattermost_channel_id, "private-channel-id")
        self.assertEqual(response.mattermost_channel_name, "lr-test-room")
        self.assertEqual(len(db.added), 1)
        self.assertIsInstance(db.added[0], BusinessRoom)
        sync.assert_called_once_with(db, db.added[0])

    def test_non_owner_cannot_provision_or_sync_a_room(self):
        db = _Session([None], self.business)
        with patch("backend.app.api.mattermost.sync_business_room") as sync:
            with self.assertRaises(HTTPException) as raised:
                create_or_sync_business_room(
                    str(self.business_id), principal=self.principal, db=db,
                )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(db.added, [])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
