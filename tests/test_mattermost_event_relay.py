"""Security and idempotency tests for authenticated Mattermost post events."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from backend.app.models import MattermostRelayEvent
from backend.app.services.mattermost_event_relay import (
    RelayOutcome,
    deliver_outcome,
    parse_posted_event,
    process_authenticated_post,
    run_authenticated_connection,
    run_worker,
    _command_kind,
    _execute_command,
)


def _frame(*, post_id="post-123", channel_id="channel-1", user_id="mm-user-1", message="/reconcile"):
    import json

    return {
        "event": "posted",
        "data": {"post": json.dumps({
            "id": post_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "message": message,
        })},
        "broadcast": {"channel_id": channel_id, "user_id": "relay-bot"},
    }


class FakeSession:
    def __init__(self, scalar_results=(), *, fail_first_commit=False):
        self.results = deque(scalar_results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_first_commit = fail_first_commit
        self.business = SimpleNamespace(id=uuid4(), is_demo=False)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1
        if self.fail_first_commit and self.commits == 1:
            raise IntegrityError("insert", {}, RuntimeError("duplicate"))

    def rollback(self):
        self.rollbacks += 1

    def scalar(self, _statement):
        return self.results.popleft() if self.results else None

    def get(self, _model, _id):
        return self.business


class MattermostEventParsingTests(unittest.TestCase):
    def test_reads_sender_channel_event_id_and_message_from_post_payload(self):
        event = parse_posted_event(_frame())
        self.assertIsNotNone(event)
        self.assertEqual((event.post_id, event.channel_id, event.user_id, event.message),
                         ("post-123", "channel-1", "mm-user-1", "/reconcile"))

    def test_rejects_mismatched_broadcast_channel(self):
        frame = _frame()
        frame["broadcast"]["channel_id"] = "other-channel"
        self.assertIsNone(parse_posted_event(frame))


class MattermostRelayAuthorizationTests(unittest.TestCase):
    server = "https://mattermost.example.test"

    def _linked_session(self, *, membership_role="owner", membership=True):
        room = SimpleNamespace(id=uuid4(), business_id=uuid4(), mattermost_status="ready")
        identity = SimpleNamespace(user_id=uuid4())
        member = SimpleNamespace(role=membership_role) if membership else None
        return FakeSession((room, identity, member)), room, identity

    def test_unauthenticated_transport_is_rejected_before_database_access(self):
        db = FakeSession()
        result = process_authenticated_post(
            db, _frame(), transport_authenticated=False, mattermost_server=self.server,
        )
        self.assertEqual(result.result_code, "unauthenticated_transport")
        self.assertEqual(db.added, [])

    def test_relay_ignores_its_own_mattermost_posts(self):
        db = FakeSession()
        result = process_authenticated_post(
            db, _frame(), transport_authenticated=True, mattermost_server=self.server,
            ignored_user_ids=frozenset({"mm-user-1"}),
        )
        self.assertEqual(result.result_code, "bot_self_message")
        self.assertEqual(db.added, [])

    def test_unlinked_mattermost_user_is_rejected(self):
        room = SimpleNamespace(id=uuid4(), business_id=uuid4(), mattermost_status="ready")
        db = FakeSession((room, None))
        result = process_authenticated_post(
            db, _frame(), transport_authenticated=True, mattermost_server=self.server,
        )
        self.assertEqual(result.result_code, "unlinked_user")
        self.assertEqual(db.added[0].status, "rejected")

    def test_wrong_room_membership_is_rejected(self):
        room = SimpleNamespace(id=uuid4(), business_id=uuid4(), mattermost_status="ready")
        identity = SimpleNamespace(user_id=uuid4())
        db = FakeSession((room, identity, None))
        result = process_authenticated_post(
            db, _frame(), transport_authenticated=True, mattermost_server=self.server,
        )
        self.assertEqual(result.result_code, "inactive_or_wrong_room_membership")
        self.assertEqual(db.added[0].business_room_id, room.id)

    def test_active_accountant_cannot_run_owner_only_reconciliation(self):
        db, _room, _identity = self._linked_session(membership_role="accountant")
        result = process_authenticated_post(
            db, _frame(), transport_authenticated=True, mattermost_server=self.server,
        )
        self.assertEqual(result.result_code, "role_forbidden")
        self.assertIn("not permitted", result.reply)
        self.assertEqual(db.added[0].status, "rejected")

    def test_accountant_can_read_reports_and_clarifications(self):
        room = SimpleNamespace(business_id=uuid4())
        business = SimpleNamespace(id=room.business_id)
        user_id = uuid4()
        with patch("backend.app.services.mattermost_event_relay._report", return_value="report"), \
             patch("backend.app.services.mattermost_event_relay._clarifications", return_value="clarifications"):
            self.assertEqual(_execute_command(
                FakeSession(), command=_command_kind("/report 2026-10"),
                room=room, user_id=user_id, role="accountant", business=business,
            ), ("completed", "report"))
            self.assertEqual(_execute_command(
                FakeSession(), command=_command_kind("/clarifications"),
                room=room, user_id=user_id, role="accountant", business=business,
            ), ("completed", "clarifications"))

    def test_accountant_cannot_reconcile_or_respond(self):
        room = SimpleNamespace(business_id=uuid4())
        business = SimpleNamespace(id=room.business_id)
        for message in ("/reconcile", f"/respond {uuid4()} Reviewed"):
            with self.subTest(message=message), self.assertRaises(PermissionError):
                _execute_command(
                    FakeSession(), command=_command_kind(message), room=room,
                    user_id=uuid4(), role="accountant", business=business,
                )

    def test_owner_reconcile_and_respond_dispatch_to_existing_services(self):
        room = SimpleNamespace(business_id=uuid4())
        business = SimpleNamespace(id=room.business_id)
        user_id = uuid4()
        request_id = uuid4()
        with patch("backend.app.services.mattermost_event_relay._reconcile", return_value="reconciled") as reconcile, \
             patch("backend.app.services.mattermost_event_relay._respond", return_value="responded") as respond:
            self.assertEqual(_execute_command(
                FakeSession(), command=_command_kind("/reconcile"), room=room,
                user_id=user_id, role="owner", business=business,
            ), ("completed", "reconciled"))
            self.assertEqual(_execute_command(
                FakeSession(), command=_command_kind(f"/respond {request_id} Paid by card"),
                room=room, user_id=user_id, role="owner", business=business,
            ), ("completed", "responded"))
        reconcile.assert_called_once()
        respond.assert_called_once_with(
            unittest.mock.ANY, room.business_id, user_id, request_id, "Paid by card",
        )

    def test_linked_owner_command_is_dispatched_with_trusted_mapped_identity(self):
        db, _room, identity = self._linked_session()
        with patch("backend.app.services.mattermost_event_relay._execute_command", return_value=("completed", "done")) as run:
            result = process_authenticated_post(
                db, _frame(), transport_authenticated=True, mattermost_server=self.server,
            )
        self.assertEqual(result.reply, "done")
        self.assertEqual(result.post_id, "post-123")
        self.assertEqual(run.call_args.kwargs["user_id"], identity.user_id)

    def test_all_supported_commands_reach_the_expected_backend_dispatch(self):
        cases = (
            ("/report 2026-10", ("report", ("2026", "10"))),
            ("/clarifications", ("clarifications", ())),
            (f"/respond {uuid4()} Paid by card", ("respond", None)),
            ("/reconcile", ("reconcile", ())),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                command = _command_kind(message)
                self.assertIsNotNone(command)
                self.assertEqual(command[0], expected[0])
                if expected[1] is not None:
                    self.assertEqual(command, expected)

    def test_plain_message_prefix_routes_supported_commands_through_post_events(self):
        request_id = uuid4()
        cases = (
            ("lr report 2026-10", ("report", ("2026", "10"))),
            ("LR clarifications", ("clarifications", ())),
            ("lr reconcile", ("reconcile", ())),
            (f"lr respond {request_id} Paid by card", ("respond", (str(request_id), "Paid by card"))),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(_command_kind(message), expected)

    def test_prefixed_post_replies_to_its_verified_originating_channel(self):
        db, _room, identity = self._linked_session()
        source_channel = "mapped-private-finance-channel"
        frame = _frame(channel_id=source_channel, message="lr report 2026-10")
        with patch("backend.app.services.mattermost_event_relay._execute_command", return_value=("completed", "report ready")) as run:
            outcome = process_authenticated_post(
                db, frame, transport_authenticated=True, mattermost_server=self.server,
            )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.channel_id, source_channel)
        self.assertEqual(outcome.reply, "report ready")
        self.assertEqual(run.call_args.kwargs["user_id"], identity.user_id)

    def test_bare_report_and_unprefixed_chat_are_not_commands(self):
        self.assertIsNone(_command_kind("report"))
        self.assertIsNone(_command_kind("please run a report"))
        self.assertIsNone(_command_kind("lr"))

    def test_prefixed_command_in_unmapped_channel_is_rejected_without_dispatch(self):
        db = FakeSession((None,))
        with patch("backend.app.services.mattermost_event_relay._execute_command") as run:
            outcome = process_authenticated_post(
                db, _frame(channel_id="public-switch-channel", message="lr report"),
                transport_authenticated=True, mattermost_server=self.server,
            )
        self.assertEqual((outcome.status, outcome.result_code), ("rejected", "unknown_channel"))
        run.assert_not_called()

    def test_duplicate_event_id_never_dispatches(self):
        db = FakeSession(fail_first_commit=True)
        with patch("backend.app.services.mattermost_event_relay._execute_command") as run:
            result = process_authenticated_post(
                db, _frame(), transport_authenticated=True, mattermost_server=self.server,
            )
        self.assertEqual(result.status, "duplicate")
        run.assert_not_called()

    def test_failed_agent_reply_is_recorded(self):
        db = FakeSession()
        row = MattermostRelayEvent(
            mattermost_server=self.server, mattermost_post_id="post-123",
            mattermost_channel_id="channel-1", mattermost_user_id="mm-user-1",
            status="processing",
        )
        db.added.append(row)
        db.scalar = lambda _statement: row
        result = deliver_outcome(
            db,
            RelayOutcome("completed", "operation_completed", "channel-1", "done", "post-123"),
            post_reply=lambda _channel, _message: (_ for _ in ()).throw(RuntimeError("Mattermost down")),
            mattermost_server=self.server,
        )
        self.assertEqual(result.result_code, "reply_delivery_failed")
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.result_code, "reply_delivery_failed")


class MattermostRelayWorkerTests(unittest.IsolatedAsyncioTestCase):
    server = "https://mattermost.example.test"

    async def test_websocket_waits_for_authentication_before_dispatching_events(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.frames = iter((
                    json.dumps(_frame(message="/report")),
                    json.dumps({"seq_reply": 1, "status": "OK"}),
                    json.dumps(_frame(message="/report")),
                    RuntimeError("test connection closed"),
                ))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, frame):
                self.sent.append(json.loads(frame))

            async def recv(self):
                value = next(self.frames)
                if isinstance(value, BaseException):
                    raise value
                return value

        socket = Socket()
        with patch("backend.app.services.mattermost_event_relay.websockets.connect", return_value=socket), \
             patch("backend.app.services.mattermost_event_relay._process_and_reply", return_value=RelayOutcome("ignored", "not_a_ledgerroom_command")) as dispatch:
            with self.assertRaisesRegex(RuntimeError, "test connection closed"):
                await run_authenticated_connection(
                    server=self.server, token="secret", session_factory=lambda: None,
                )
        self.assertEqual(socket.sent[0]["action"], "authentication_challenge")
        self.assertEqual(socket.sent[0]["data"]["token"], "secret")
        dispatch.assert_called_once()

    async def test_worker_reconnects_after_connection_failure(self):
        settings = SimpleNamespace(MATTERMOST_RELAY_ENABLED=True, MATTERMOST_SWITCH_BOT_USER_ID=None)
        with patch("backend.app.services.mattermost_event_relay.service_configuration", return_value=(self.server, "team", "token")), \
             patch("backend.app.services.mattermost_event_relay.get_session_factory", return_value=lambda: None), \
             patch("backend.app.services.mattermost_event_relay._service_account_user_id", return_value="relay-bot"), \
             patch("backend.app.services.mattermost_event_relay.run_authenticated_connection", side_effect=(ConnectionError("lost"), asyncio.CancelledError())) as connect, \
             patch("backend.app.services.mattermost_event_relay.asyncio.sleep", new=unittest.mock.AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await run_worker(settings)
        self.assertEqual(connect.call_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_worker_ignores_both_its_service_user_and_switch_bot(self):
        settings = SimpleNamespace(MATTERMOST_RELAY_ENABLED=True, MATTERMOST_SWITCH_BOT_USER_ID="switch-bot")
        with patch("backend.app.services.mattermost_event_relay.service_configuration", return_value=(self.server, "team", "token")), \
             patch("backend.app.services.mattermost_event_relay.get_session_factory", return_value=lambda: None), \
             patch("backend.app.services.mattermost_event_relay._service_account_user_id", return_value="relay-bot"), \
             patch("backend.app.services.mattermost_event_relay.run_authenticated_connection", side_effect=asyncio.CancelledError()) as connect:
            with self.assertRaises(asyncio.CancelledError):
                await run_worker(settings)
        self.assertEqual(connect.call_args.kwargs["ignored_user_ids"], frozenset({"relay-bot", "switch-bot"}))


if __name__ == "__main__":
    unittest.main()
