# Mattermost integration setup

LedgerRoom keeps the business room, membership, invitation and permission records. Mattermost holds the shared channel transcript. The channel ID is mapped to the LedgerRoom room ID; Mattermost identity IDs are only linked after the signed-in LedgerRoom user completes Mattermost OAuth.

## Required Mattermost setup

1. The local Switch-managed Mattermost service is at `http://127.0.0.1:45168`. The configured LedgerRoom service account authenticates successfully and can access the configured team. The values live only in the ignored development environment file; do not print or copy them into tracked files.
2. Create a bot/service account for LedgerRoom and grant it the permissions needed to create private channels, add team members, and post. Configure its personal access token as a backend secret.
3. Register a **confidential OAuth application** in Mattermost. Enable the OAuth 2.0 service provider and set its callback URL to the exact `MATTERMOST_OAUTH_CALLBACK_URL`. PKCE S256 is used by the LedgerRoom authorization-code flow.
4. Configure Switch's Mattermost adapter for the same server and team. Set the Mattermost user ID used by the Switch agent/bot so LedgerRoom can add it to each private channel. The Switch runtime configuration is not present in this repository; use the settings supported by the deployed Switch release rather than copying guessed environment-variable names.

Configure these backend environment variables (keep secrets out of source control):

```text
MATTERMOST_BASE_URL=https://mattermost.example.com
MATTERMOST_TEAM_ID=<team-id>
MATTERMOST_BOT_TOKEN=<service-account-personal-access-token>
MATTERMOST_SWITCH_BOT_USER_ID=<mattermost-user-id-for-switch-agent>
MATTERMOST_OAUTH_CLIENT_ID=<oauth-client-id>
MATTERMOST_OAUTH_CLIENT_SECRET=<oauth-client-secret>
MATTERMOST_OAUTH_CALLBACK_URL=https://ledgerroom.example.com/api/v1/integrations/mattermost/callback
AUTH_PUBLIC_BASE_URL=https://ledgerroom.example.com
MATTERMOST_RELAY_ENABLED=true
```

`MATTERMOST_BASE_URL` may use HTTP only when `APP_ENV` is `development`, `local`, or `test`; production requires HTTPS. The local backend must use `LEDGERROOM_CONFIG_FILE=backend/.env.mattermost-development` and the isolated development database. Keep `backend/.env` and its production settings unchanged. The authenticated team lookup succeeds, but channel create/member-write permissions have not yet been demonstrated by a real business workflow.

### Current local readiness

The local service account authenticates, accesses the configured team, and can read the OAuth application's public information. The OAuth app still allows the old port-8000 callback; the development backend is configured for port 8010. The service account is not a System Admin, and the server does not advertise dynamic client registration. The relay remains disabled. Do not edit Switch Core storage or copy Switch sign-in credentials into LedgerRoom.

Switch Console 0.40.1 shows the existing `ledgerroom` agent and a connected Mattermost component. Switch Core receives authenticated Mattermost posts with the platform sender, channel, post, and thread identifiers. Its owner-only addressing policy resolves the Mattermost sender to a Switch user through a separate identity claim in Switch Console under **Messaging apps**; LedgerRoom's OAuth mapping does not create this Switch claim. The claim can allow the real agent to address its owner, but the stdio MCP handler still receives only the Switch agent/session context, not the originating human sender. Keep LedgerRoom MCP financial tools fail-closed until Switch provides trusted per-message identity to the tool call or a supported delegated credential that LedgerRoom can independently validate. The direct LedgerRoom WebSocket relay remains the trusted path for explicit plain-post commands prefixed with `lr` (`lr report`, `lr clarifications`, `lr reconcile`, `lr respond ...`); Mattermost treats a leading `/` as a native slash command and sends it to its command execution API, so LedgerRoom does not claim `/report` is supported unless a real native command is separately registered. The relay authenticates its Mattermost connection, resolves sender and channel from post events, checks LedgerRoom identity/membership/role, and replies in that mapped channel.

## Isolated local development setup

The normal settings loader still defaults to `backend/.env`. For a local-only run, set `LEDGERROOM_CONFIG_FILE` to `backend/.env.mattermost-development`; this selects the same file for application settings and `DATABASE_URL`. The selected config file is ignored by Git. A tracked placeholder template is at `backend/.env.mattermost-development.example`. In `development` or `local` mode, the database loader now rejects non-loopback hosts, any port other than 55432, and any database other than `ledgerroom_mattermost_dev`. This prevents a local config from silently selecting a remote/production database. Loopback PostgreSQL may use its local no-TLS connection; remote/production connections still require TLS.

The separate `docker-compose.mattermost-dev.yml` file creates only `ledgerroom-mattermost-dev-db`, binds it to `127.0.0.1:55432`, and uses the distinct volume `ledgerroom_mattermost_dev_pgdata`. Its database name and user are `ledgerroom_mattermost_dev` and `ledgerroom_dev`. Copy both `backend/.env.mattermost-development.example` and `backend/.env.mattermost-development-db.example` to their corresponding ignored local filenames. Set the same fresh local password in the app config's `DATABASE_URL` and the DB-only config's `POSTGRES_PASSWORD`. The DB container receives only the DB password, not Mattermost or OAuth credentials. Use an alphanumeric/hex password so it does not require URL escaping. Do not copy production `DATABASE_URL` into this file.

For local development, set `AUTH_PUBLIC_BASE_URL=http://127.0.0.1:5173` because email verification and accountant invitation links are served by the frontend. Set `MATTERMOST_OAUTH_CALLBACK_URL=http://127.0.0.1:8010/api/v1/integrations/mattermost/callback` in the backend config and add that exact URL to the Mattermost OAuth application's authorized callback list. LedgerRoom sends the configured callback as `redirect_uri` to `/oauth/authorize` and repeats it when exchanging the code. Mattermost must have its OAuth 2.0 service provider enabled and that exact callback registered. The configured bot token can read the OAuth app's public info, but is not a System Admin; the current app still allows the old port-8000 callback. An app creator with `manage_oauth` or a System Admin must update the existing app's callback list while preserving its client ID and secret. DCR is not advertised by this server, so it is not a supported workaround here.

### Obtain Mattermost values through supported interfaces

- **Team ID:** sign into the Mattermost web UI and open the target team's details/settings, or call the supported `GET /api/v4/teams` endpoint using an authorized account. The configured service account can currently access the configured team.
- **LedgerRoom bot token and bot user ID:** create/use a dedicated Mattermost bot or service account through Mattermost System Console's integrations/bot-account interface. Create a personal access token through Mattermost's supported account/API access-token controls. Obtain its user ID from the bot account's profile or authenticated `GET /api/v4/users/me`. Grant only the permissions the integration needs: create private channels, add channel members, and post.
- **Switch bot user ID:** use the Switch Mattermost adapter's configured bot account identity as shown in its supported settings/UI. Do not read Switch Core storage or reuse Switch credentials as LedgerRoom's service token.
- **OAuth client ID/secret:** enable OAuth 2.0 service-provider support and register a confidential application in Mattermost's supported admin interface. Add the exact callback above to its authorized redirect URI list. Copy the displayed client ID and secret into the local-only config file; do not commit or print them.

Keep `MATTERMOST_RELAY_ENABLED=false` until the local team, bot, OAuth registration, and local database are ready. Then set it to `true` in the ignored development config. No production settings, Mattermost server, Switch Core data, or database migrations are changed by this setup.

## Flow

1. An authenticated user calls `POST /api/v1/integrations/mattermost/link` and follows the returned authorization URL. The callback consumes a short-lived, one-time state bound to that LedgerRoom user, exchanges the OAuth code, and reads the verified identity from Mattermost `/api/v4/users/me`. OAuth access tokens are discarded; only the verified Mattermost user ID is stored in `external_identities`.
2. A business owner calls `POST /api/v1/businesses/{business_id}/room`. LedgerRoom creates its UUID room record. If the owner has linked Mattermost and the service account is configured, it provisions a private channel; otherwise the room records a pending status and can be retried by calling the same endpoint after setup.
3. The existing owner-only invitation endpoint returns a `shareable_invitation_link` in its response. The owner can share this one-time, seven-day link through WhatsApp. The link is not a substitute for authentication: acceptance still requires the invited email's authenticated LedgerRoom account.
4. After an accountant accepts, LedgerRoom synchronizes active linked identities into the mapped channel. If the accountant has not linked Mattermost yet, the room status stays partial. Completing OAuth later retries membership synchronization automatically.
5. `MATTERMOST_SWITCH_BOT_USER_ID` must resolve to the account used by Switch's Mattermost adapter. LedgerRoom adds that user to the channel. Switch's connected agent then receives/posts channel events through its Mattermost adapter.
6. Run the separate backend relay worker under a supervisor: `python -m backend.app.services.mattermost_event_relay`. It resolves its own Mattermost user ID through the authenticated `GET /api/v4/users/me` API, opens `/api/v4/websocket`, authenticates with the configured service token, and waits for Mattermost's authentication response before consuming `posted` events. It ignores posts from both its own account and `MATTERMOST_SWITCH_BOT_USER_ID`. To invoke the supported deterministic commands, send ordinary channel posts beginning with `lr`: `lr report [YYYY-MM]`, `lr clarifications`, `lr reconcile`, or `lr respond <request-id> <answer>`. Other messages remain available to Switch's agent.

The live database was verified at revision 20261107_0017; both Mattermost migrations are applied. The local Switch/Codex stdio MCP configuration does not provide verified caller metadata and points at a different checkout, so MCP financial tools remain fail-closed. The authenticated Mattermost WebSocket worker handles explicit commands directly; general natural-language requests still depend on a correctly configured Switch agent and are not authorized through MCP by this worker.
