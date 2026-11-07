import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr

from backend.app.core.config import Settings
from backend.app.db.session import DatabaseConfigurationError, get_database_url


class LocalDatabaseConfigurationTests(unittest.TestCase):
    def database_url(self, raw_url: str, app_env: str):
        with (
            patch(
                "backend.app.db.session.get_database_settings",
                return_value=SimpleNamespace(DATABASE_URL=SecretStr(raw_url)),
            ),
            patch(
                "backend.app.db.session.get_settings",
                return_value=Settings(_env_file=None, APP_ENV=app_env),
            ),
        ):
            get_database_url.cache_clear()
            return get_database_url()

    def test_local_development_database_does_not_require_tls(self):
        url = self.database_url(
            "postgresql://ledgerroom_dev:localpassword@127.0.0.1:55432/ledgerroom_mattermost_dev",
            "development",
        )
        self.assertNotIn("sslmode", url.query)

    def test_production_database_still_requires_tls(self):
        url = self.database_url(
            "postgresql://ledgerroom_user:secret@db.example.com:5432/ledgerroom",
            "production",
        )
        self.assertEqual(url.query["sslmode"], "require")

    def test_development_remote_database_still_requires_tls(self):
        with self.assertRaisesRegex(
            DatabaseConfigurationError,
            "Local development must use the isolated loopback LedgerRoom Mattermost database",
        ):
            self.database_url(
                "postgresql://ledgerroom_dev:localpassword@db.example.com:5432/ledgerroom",
                "development",
            )

    def test_development_production_database_name_is_rejected(self):
        with self.assertRaisesRegex(
            DatabaseConfigurationError,
            "Local development must use the isolated loopback LedgerRoom Mattermost database",
        ):
            self.database_url(
                "postgresql://ledgerroom_dev:localpassword@127.0.0.1:55432/ledgerroom",
                "development",
            )


if __name__ == "__main__":
    unittest.main()
