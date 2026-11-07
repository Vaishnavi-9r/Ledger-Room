# LedgerRoom Production Readiness Audit

**Audit date:** 2026-11-06  
**Decision:** **NOT READY for production launch.** The implementation has strong tenant and financial safeguards, but several operational and integration controls are not configured or verified.

## Scope and verification

- Inspected the backend, MCP server, migrations, tests, ignore rules, and installed Switch resource layout.
- Connected to the configured PostgreSQL database for `alembic current` and confirmed revision `20261106_0013 (head)`; both `monthly_report_approvals` and `monthly_report_approval_events` exist.
- No migration was run. This audit did not create, update, or delete database records, send email, call Gemini, or contact Slack.
- Backend unittest suite: **142 passed**. MCP unittest suite: **13 passed**. `pip check` found no broken requirements, but emitted a warning about an orphaned `~ip-25.0.1.dist-info` directory in the virtual environment.
- Email/Gemini/JWT configuration was checked only for presence. No secret or setting value was displayed. SMTP is not configured; JWT and Gemini keys are configured.
- Switch is installed as an Electron package with its main code in `resources/app.asar`; the unpacked application tree contains runtime dependencies rather than provider source. The repository's MCP implementation and documentation were inspected. No supported trusted Slack identity handoff was verified.

## Prioritized findings

| Severity | Finding | Evidence and impact | Recommended action |
|---|---|---|---|
| **High** | Accountant email onboarding is unavailable in this environment. | SMTP host, sender, credentials, and public base URL are absent. `get_email_provider()` returns no provider; invitation creation responds with a safe 503. Verification links are therefore not delivered either. Token generation, hash-only storage, seven-day expiry, revocation, and single-use acceptance are implemented. | Configure SMTP and the public verification URL in a staging environment, then verify delivery, bounce handling, and acceptance end to end before enabling customer onboarding. No email was sent during this audit. |
| **High** | Invoice and receipt storage is local disk without a durable storage, quota, or retention policy. | Files are stored under `data/invoices/` and `data/receipts/`; each request is capped at 10 MB and filenames/path resolution are constrained. There is no aggregate tenant quota, cleanup/retention job, orphan-file scan, or demonstrated backup/restore path. A host loss or disk exhaustion can make evidence unavailable. | Use durable, access-controlled object storage with backup/lifecycle policy, per-business capacity controls, monitoring, and orphan reconciliation. Verify restore before launch. |
| **High** | Rate limiting is process-local. | `ProcessRateLimiter` keeps its window in memory per Python process. Login, registration, verification, and invitation limits can be multiplied across workers or replicas and reset on restart. | Add shared ingress or distributed rate limiting, configure trusted proxy/client-IP handling, and test limits across multiple workers. |
| **High** | Slack MCP actions are intentionally unavailable until a trusted caller identity bridge exists. | `mcp_server/server.py` returns `identity_unavailable` without calling the backend. `backend_client.py` has no authentication header or business context. Its helper requests cannot satisfy the backend's authenticated-membership dependencies. Switch's installed package is bundled in `app.asar`; no supported per-invocation Slack identity interface was verified from locally inspectable source. | Keep the current fail-closed behavior. Do not pass user/business/role as tool arguments or configure a shared owner JWT. Implement only after Switch documents/provides verified Slack workspace and user context, or after a trusted signed server-side relay is designed and reviewed. |
| **High** | Database backup and restore readiness is not evidenced by the repository. | The application connects successfully and uses SSL, pooling, and a database health probe, but backup/PITR settings and a restore drill are external operational controls not established by this audit. | Confirm Supabase backup/PITR coverage and complete a restore rehearsal into a separate test project before production. |
| **Medium** | Uploaded request limits are enforced after multipart parsing. | The handlers read at most 10 MB plus one byte from `UploadFile`; there is no application-wide streaming body limit visible in the API setup. Multipart parsing may spool larger bodies before endpoint validation. | Set request-body limits at the trusted ingress/reverse proxy and verify temporary-file limits. Keep endpoint validation as defense in depth. |
| **Medium** | Stale approval is derived on read, not immediately persisted as an event. | Status/export endpoints compare the current report fingerprint and return `stale`. The stored approval row and audit event are updated when a later submit/review operation detects the change, not during read-only status/export. Consumers querying the table directly can still see the prior stored status in the interim. | Either document API status as authoritative or add an explicit transactional stale-refresh mechanism that records the transition without making report reads mutate state. |
| **Medium** | Approval/audit history is append-only at the application API level, not tamper-proof at the database level. | No ordinary API edits/deletes these events, and foreign keys use `RESTRICT`, but the database role can still alter/delete event rows unless privileges or database triggers prevent it. | Restrict table privileges and/or add reviewed database-level immutability controls; define retention and audit export procedures. |
| **Medium** | Password hashing differs from the earlier Argon2 requirement. | The implementation uses scrypt (`N=2^15`, `r=8`, `p=1`) with random salts and constant-time digest comparison. This is a password-hardening algorithm, but it is not Argon2id and there is no visible versioned migration/rehash path. | Decide and document the supported password-hash policy. If changing to Argon2id, implement dual verification and rehash-on-login rather than invalidating existing hashes. |
| **Medium** | Dependency versions are not fully locked and no vulnerability database scan was run. | Several entries in `requirements.txt` are unpinned. `pip check` found no dependency conflicts, but it is not a CVE audit; the environment also has an orphaned pip metadata directory. | Add a reproducible lock/constraints file, run a maintained dependency vulnerability scanner in CI, and clean the orphaned environment artifact during routine environment maintenance. |
| **Low** | CORS policy is absent. | `main.py` does not configure CORS. Same-origin clients work; a separately hosted browser client will be blocked or may prompt an unsafe wildcard workaround. | If a cross-origin frontend is planned, add an explicit allowlist of its origins and test preflight/auth behavior. |
| **Low** | Readiness is split across liveness and a database-only probe. | `/health` is process liveness; `/api/v1/health/database` executes `SELECT 1`. The DB endpoint does not validate migration head or Gemini/email availability. | Keep liveness lightweight; have deployment readiness check the DB endpoint and migration state. Treat optional provider readiness separately from core API readiness. |

## Controls verified

- **Invitations:** random opaque bearer token, hash stored in PostgreSQL, seven-day expiration, revocation, accepted/revoked state checks, email ownership check, active verified account requirement, owner-only creation/list/revocation, and database uniqueness protection for pending invitations. SMTP delivery is deliberately reported only as provider submission and is not configured here.
- **Authentication:** JWT issuer/audience/expiration validation with a configured signing key; active and verified account checks; refresh-token hashes, family rotation, replay revocation, and logout family revocation. Passwords are never stored in plain text. Login errors are generic. Request validation strips submitted input from error responses. Access-token families are checked against live refresh-token state.
- **Uploads:** JPG/JPEG/PNG extension and Pillow content validation, 10 MB per-file cap, UUID filenames, exclusive file creation, tenant-scoped metadata lookups, path traversal checks, and cleanup after ordinary database persistence failures. Crash-window orphan files and aggregate storage limits remain operational risks.
- **Financial integrity:** all persisted calculations use integer paise; rupee parsing uses `Decimal`. Bank CSV imports are transactional and deduplicate on normalized date/description/signed amount/reference under a PostgreSQL advisory lock. Reconciliation is deterministic, excludes credits, uses transaction locking, prevents bank-line reuse, and records changes in match events. Receipt verification is deterministic and appends a `ChaseEvent` only when the result changes.
- **Approval:** current revision is applied; submissions are versioned and business-scoped, pending submissions have a partial unique index, accountant actions lock the approval row, self-review is rejected, and fingerprint changes are surfaced as stale. The live test verified submit, request-changes, resubmit, approve, exports, and post-approval change detection using temporary synthetic accounts/business/invoice records; all test records were removed.
- **Database:** SQLAlchemy sessions close per request; PostgreSQL SSL is required, parameters are hidden in SQLAlchemy logs, and pool settings are configured. `/api/v1/health/database` is a safe `SELECT 1` probe.
- **MCP:** current tools do not accept identity arguments and fail closed. This is the correct security behavior until a trusted identity transport exists; it means the Slack-connected workflow is not presently operational.

## Important test gaps

The passing unit suites do not replace these staging/operational checks:

1. Real SMTP delivery, invitation acceptance from a separately registered/verifying accountant, expiry, revoke, and replay against the configured provider.
2. Multi-process rate-limit behavior and trusted proxy handling.
3. Concurrent duplicate CSV imports, reconciliation runs, and approval reviews against PostgreSQL under load (the application uses advisory/row locks, but full deployment-level load tests are absent).
4. Upload size enforcement at the ingress, disk exhaustion behavior, process-crash orphan cleanup, storage backup, and restore.
5. A real Gemini canary using synthetic documents, provider quota/timeout handling, and redaction review.
6. A real Switch/Slack end-to-end test with an authenticated workspace/user identity. No supported identity context currently reaches the MCP handler, so this must remain blocked rather than simulated with model-supplied arguments.
7. A maintained dependency CVE scan and Supabase restore drill.

## Files changed in this audit

- `.gitignore`: added `/data/invoices/` and `/data/receipts/` so stored financial evidence is not accidentally added in future commits. This does not untrack files that were already committed.
- `PRODUCTION_READINESS_AUDIT.md`: this audit report.

No application behavior, database schema, migration, production record, email, Slack setting, or MCP configuration was changed.
