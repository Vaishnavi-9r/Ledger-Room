import unittest

from backend.app.core.config import Settings
from backend.app.services.mattermost_service import (
    MattermostError,
    canonical_server_url,
    oauth_configuration,
)


class MattermostDevelopmentConfigurationTests(unittest.TestCase):
    def test_local_http_server_and_callback_are_accepted_only_for_development(self):
        development = Settings(
            _env_file=None,
            APP_ENV="development",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
            MATTERMOST_OAUTH_CLIENT_ID="local-client-id",
            MATTERMOST_OAUTH_CLIENT_SECRET="local-client-secret",
            MATTERMOST_OAUTH_CALLBACK_URL=(
                "http://127.0.0.1:8010/api/v1/integrations/mattermost/callback"
            ),
        )
        self.assertEqual(canonical_server_url(development), "http://127.0.0.1:45168")
        self.assertEqual(
            oauth_configuration(development),
            ("http://127.0.0.1:45168", "local-client-id", "local-client-secret"),
        )

        production = Settings(
            _env_file=None,
            APP_ENV="production",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
        )
        with self.assertRaises(MattermostError) as error:
            canonical_server_url(production)
        self.assertEqual(error.exception.code, "invalid_configuration")

    def test_local_oauth_callback_requires_credentials_and_well_formed_url(self):
        missing_credentials = Settings(
            _env_file=None,
            APP_ENV="development",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
            MATTERMOST_OAUTH_CALLBACK_URL=(
                "http://127.0.0.1:8010/api/v1/integrations/mattermost/callback"
            ),
        )
        with self.assertRaises(MattermostError) as error:
            oauth_configuration(missing_credentials)
        self.assertEqual(error.exception.code, "oauth_not_configured")

        malformed_callback = Settings(
            _env_file=None,
            APP_ENV="development",
            MATTERMOST_BASE_URL="http://127.0.0.1:45168",
            MATTERMOST_OAUTH_CLIENT_ID="local-client-id",
            MATTERMOST_OAUTH_CLIENT_SECRET="local-client-secret",
            MATTERMOST_OAUTH_CALLBACK_URL="/api/v1/integrations/mattermost/callback",
        )
        with self.assertRaises(MattermostError) as error:
            oauth_configuration(malformed_callback)
        self.assertEqual(error.exception.code, "invalid_configuration")


if __name__ == "__main__":
    unittest.main()
