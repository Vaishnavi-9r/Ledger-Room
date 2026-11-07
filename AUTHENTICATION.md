# Authentication configuration and local verification

The existing authentication API uses the `User` and `AuthToken` models. Set configuration in the process environment or `backend/.env`; do not commit secrets. No secrets are supplied by the repository.

## Required configuration

Set `AUTH_JWT_SIGNING_KEY` to a randomly generated secret with at least 32 UTF-8 bytes. The API intentionally refuses to issue or validate access tokens without it. Keep the value private and use a different key for each environment.

`AUTH_REQUIRE_EMAIL_VERIFICATION` defaults to `true`, including production. The application rejects disabling it unless `APP_ENV` is `development`, `local`, or `test`. Access tokens default to 900 seconds (`AUTH_ACCESS_TOKEN_TTL_SECONDS`). Refresh-token families default to 30 days (`AUTH_REFRESH_TOKEN_TTL_DAYS`). Verification links default to 24 hours (`AUTH_EMAIL_VERIFICATION_TTL_HOURS`).

## Local development without mandatory verification

Set this process environment configuration for a local run:

```dotenv
APP_ENV=development
AUTH_REQUIRE_EMAIL_VERIFICATION=false
```

Registration leaves `email_verified_at` unset and responds with `email_verification_required: false`; the user can sign in immediately with the password they registered. Password hashing, JWT validation, refresh-token family checks, logout revocation, account activity checks, and business membership authorization still apply. The email verification and resend endpoints remain available to re-enable the required flow later. No verification email is sent when verification is not required.

## Development email mode when verification is required

For a local Swagger flow, set:

```dotenv
APP_ENV=development
AUTH_REQUIRE_EMAIL_VERIFICATION=true
AUTH_DEV_RETURN_VERIFICATION_LINK=true
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:5173
```

The API returns a `development_verification_link` only when the flag is explicitly enabled, the app environment is `development`, `local`, or `test`, and the request comes from loopback. The link contains a single-use, expiring token. The frontend serves `/verify-email` on port 5173. If you want to verify entirely in Swagger, copy the token from the link’s query string and post it to `/api/v1/auth/verify-email`.

Development links are never returned in production. An account is not marked verified until the verification endpoint consumes a valid token.

## Production SMTP

Configure `SMTP_HOST`, `SMTP_PORT`, and `SMTP_FROM_EMAIL`; `AUTH_PUBLIC_BASE_URL` must be the public frontend URL used in the email link. If SMTP authentication is required, set both `SMTP_USERNAME` and `SMTP_PASSWORD`; leave both unset if the relay permits unauthenticated submission. The provider uses STARTTLS. Delivery configuration or submission failures are reported in the registration response, and the user can request another link through the resend endpoint after delivery is restored.

## Swagger registration flow

1. Open `/docs` on the running API and call `POST /api/v1/auth/register` with `full_name`, `email`, `mobile_number`, `password`, and `password_confirmation`. Use an address you control; registration creates a real account record.
2. If `AUTH_REQUIRE_EMAIL_VERIFICATION=false`, skip verification and sign in immediately. If it is `true`, copy the token from the loopback-only `development_verification_link` and call `POST /api/v1/auth/verify-email` with `{"token":"<copied-token>"}`. A valid token works once and expires after the configured lifetime.
3. Call `POST /api/v1/auth/login` with `email` and `password`. Copy `access_token` from the response.
4. Use Swagger’s **Authorize** control with `Bearer <access_token>`, then call `GET /api/v1/auth/me` and `GET /api/v1/businesses/`.
5. If the verification message did not arrive, call `POST /api/v1/auth/resend-verification` with the account email and use the newly issued link. Resending revokes the previous unused verification token.

Do not enable development-link mode in production. Do not share or log verification, access, or refresh tokens.
