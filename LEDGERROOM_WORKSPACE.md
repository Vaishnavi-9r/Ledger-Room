# LedgerRoom shared workspace

The shared room uses each business as its conversation boundary. Active business membership is resolved by the existing authentication dependency on every history and send request. Sender identity and role are taken from the authenticated membership; clients cannot set them. Invoice attachment IDs are validated against that same business.

## Local development

The message table is introduced by Alembic revision `20261107_0015` (after `20261106_0014`). Review and apply the migration separately before starting the new conversation API. No migration was applied as part of this implementation.

For local password login without email verification, set `APP_ENV=development` and `AUTH_REQUIRE_EMAIL_VERIFICATION=false` in the backend process environment. To test invitation acceptance without SMTP, also set `AUTH_DEV_RETURN_INVITATION_LINK=true` and `AUTH_PUBLIC_BASE_URL=http://127.0.0.1:5173`. The returned invitation token is exposed only in development when that explicit option is enabled; it is stored hashed and remains single-use and expiring. Production invitations continue to require a configured email provider.

Set `GEMINI_API_KEY` to enable natural language responses. The API sends the authenticated participant name, active business name, role, and the new chat message to Gemini. The language model has no tools and receives no financial records. Financial actions are explicit server-side routes: `/reconcile` uses deterministic owner-only reconciliation; `/report YYYY-MM` uses the existing scoped monthly report; `/clarifications` lists this business's pending clarification requests; `/respond <request-id> <response>` records an owner response through the existing clarification service. Invoice attachments invoke the existing extraction provider. If Gemini is not configured, explicit commands still work and free-form messages receive a clear unavailable response.

Invoice and bank statement uploads use the existing APIs and their existing owner-only authorization. The workspace polls the business conversation every five seconds so members see persisted messages without a separate realtime service. Presence indicators are not shown because the backend has no presence API.
