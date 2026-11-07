"""Migration preflight and preservation-plan tests using isolated fakes."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


MIGRATION_PATH = Path(__file__).parents[1] / "migrations" / "versions" / "20261106_0007_identity_business_tenancy.py"
SPEC = importlib.util.spec_from_file_location("identity_business_migration", MIGRATION_PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
FAMILY_PATH = Path(__file__).parents[1] / "migrations" / "versions" / "20261106_0008_auth_token_families.py"
FAMILY_SPEC = importlib.util.spec_from_file_location("auth_token_family_migration", FAMILY_PATH)
assert FAMILY_SPEC and FAMILY_SPEC.loader
family_migration = importlib.util.module_from_spec(FAMILY_SPEC)
FAMILY_SPEC.loader.exec_module(family_migration)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    def __init__(self, values=()):
        self.values = iter(values)
        self.queries = []

    def execute(self, statement):
        self.queries.append(str(statement))
        return _Result(next(self.values, 0))


class _Operations:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def get_bind(self):
        return self.connection

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


class IdentityBusinessMigrationTests(unittest.TestCase):
    def test_preflight_aborts_on_orphan_without_mutation(self):
        connection = _Connection([0, 2])
        operations = _Operations(connection)
        with patch.object(migration, "op", operations):
            with self.assertRaisesRegex(RuntimeError, "matches.invoice_id"):
                migration.upgrade()
        self.assertEqual(operations.calls, [])
        self.assertEqual(len(connection.queries), 2)

    def test_preflight_checks_all_existing_reference_paths(self):
        connection = _Connection([0] * len(migration.PREFLIGHT_QUERIES))
        migration.preflight(connection)
        self.assertEqual(len(connection.queries), len(migration.PREFLIGHT_QUERIES))
        self.assertIn("chase_events.clarification_request_id", [name for name, _ in migration.PREFLIGHT_QUERIES])

    def test_upgrade_backfill_changes_only_business_id_and_preserves_ids(self):
        connection = _Connection([0] * len(migration.PREFLIGHT_QUERIES))
        operations = _Operations(connection)
        with patch.object(migration, "op", operations):
            migration.upgrade()
        updates = [str(args[0]) for name, args, _ in operations.calls if name == "execute" and "UPDATE " in str(args[0])]
        self.assertEqual(len(updates), len(migration.FINANCIAL_TABLES))
        for sql in updates:
            self.assertIn("SET business_id", sql)
            self.assertNotIn("SET id", sql)
        create_business = next(args for name, args, _ in operations.calls if name == "create_table" and args[0] == "businesses")
        self.assertEqual(create_business[0], "businesses")
        self.assertTrue(any("LedgerRoom Demo Business" in str(args[0]) for name, args, _ in operations.calls if name == "execute"))

    def test_migration_revision_follows_existing_head(self):
        self.assertEqual(migration.revision, "20261106_0007")
        self.assertEqual(migration.down_revision, "20261105_0006")

    def test_auth_token_family_migration_is_additive_and_guarded(self):
        self.assertEqual(family_migration.revision, "20261106_0008")
        self.assertEqual(family_migration.down_revision, migration.revision)
        connection_with_refresh = _Connection([1])
        with patch.object(family_migration.op, "get_bind", return_value=connection_with_refresh), \
                patch.object(family_migration.op, "add_column") as add_column:
            with self.assertRaisesRegex(RuntimeError, "without family IDs"):
                family_migration.upgrade()
        add_column.assert_not_called()
        connection = _Connection([1])
        with patch.object(family_migration.op, "get_bind", return_value=connection), \
                patch.object(family_migration.op, "drop_index") as drop_index:
            with self.assertRaisesRegex(RuntimeError, "family/revocation history"):
                family_migration.downgrade()
        drop_index.assert_not_called()


if __name__ == "__main__":
    unittest.main()
