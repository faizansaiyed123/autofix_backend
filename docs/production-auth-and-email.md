# Production authentication and email delivery

## Required production settings

Configure these on the backend hosting service's environment page; do not commit live secrets.

- `APP_ENV=production`
- `SECRET_KEY`: a long, unique, randomly generated secret used for JWT signing. Keep it stable across running instances and never reuse a development value.
- `DATABASE_URL`: the production PostgreSQL connection string.
- `ALLOWED_ORIGINS=["https://autofix-frontend-phi.vercel.app"]`: explicit allowed browser origins as a JSON array. Add your own production domain if one is used; do not use `*`.
- `PUBLIC_APP_URL=https://autofix-frontend-phi.vercel.app`: the public frontend origin used for verification and password-reset links.
- `AUTH_RATE_LIMIT_TRUST_PROXY_HEADERS=true`: enable only when the service is reachable through Render/Cloudflare's trusted edge. In production the application refuses to start when this is missing or false so IP-based abuse controls are not silently weakened.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, and, where required, `SMTP_USERNAME` and `SMTP_PASSWORD`. Use a TLS-enabled SMTP relay you control or are authorized to use. This code uses Python's standard library and does not require a mail SDK.
- Use `SMTP_USE_STARTTLS=true` with a STARTTLS endpoint, or `SMTP_USE_SSL=true` with an implicit-TLS endpoint. Do not use both as a substitute for configuring the correct endpoint.
- `SMTP_TIMEOUT_SECONDS=10` bounds mail-server waits.
- `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, and `DATABASE_POOL_TIMEOUT` should be tuned against the PostgreSQL connection limit and number of application instances. The pool limit applies per process, so multiply it by the number of instances/workers.
- `EMAIL_OUTBOX_BATCH_SIZE`, `EMAIL_OUTBOX_POLL_SECONDS`, `EMAIL_OUTBOX_MAX_ATTEMPTS`, and `EMAIL_OUTBOX_LOCK_SECONDS` configure bounded mail throughput, polling, retries, and stale-worker recovery.

The frontend deployment must set `NEXT_PUBLIC_API_URL` to the deployed backend's HTTPS origin, without a trailing `/api/v1`; the frontend appends that API prefix itself.

## Deployment order

1. Configure the environment variables above, particularly SMTP and the explicit frontend origin.
2. Run `alembic upgrade head` against the production database before serving traffic. The migrations add email-verification and reset-token state, shared rate-limit counters, the token-minimizing email outbox, and baseline roles/permissions/grants needed by signup and the customer portal. This RBAC bootstrap does **not** create demo users or demo passwords.
3. Deploy the backend, verify `/health`, and check logs for email delivery failures and SMTP configuration. Do not run `scripts/seed.py` on a production database unless demo logins are explicitly desired; that script creates demo user accounts.
4. Deploy the frontend with `NEXT_PUBLIC_API_URL` set. Registration creates a customer account and queues a verification email; only opening a valid, unexpired link activates the account and starts a session.
5. Test one complete registration, single-use email verification, login, password-reset email, password reset, and login using the new password before opening public signups.

## What the safeguards do

- Shared PostgreSQL counters enforce IP and email limits across workers/instances; rate-limit keys are HMACed with the application secret.
- Login and recovery avoid revealing whether a specific email is registered. New customer accounts get only the `CUSTOMER` role; signup payloads cannot grant staff permissions.
- Verification/reset tokens are generated from secure random bytes; only digests are stored on the user record; tokens expire and are single use.
- Refresh tokens rotate and are stored as a digest. Logout and password changes increment an account version to revoke prior sessions.
- Email outbox rows contain only a user reference and message type—never a live verification/reset URL or message body. Tokens are generated in worker memory immediately before SMTP delivery, and only token digests are saved on the user row. Jobs are written in the same transaction as signup/reset changes. Workers claim bounded batches with `FOR UPDATE SKIP LOCKED`, do SMTP I/O outside database transactions, retry with backoff, and recover abandoned processing rows after a stale lock.
- SMTP and password hashing run off the async event loop; database pools are bounded. Security headers and explicit CORS configuration are enforced.

These controls reduce common attack paths; they do not make an internet-facing service impossible to attack. Keep the runtime and database patched, use TLS, protect and rotate secrets, back up PostgreSQL, monitor authentication failures/outbox retries, and add trusted edge/WAF rules if traffic grows or attack patterns change. Email delivery cannot work until valid SMTP settings are configured.

## Security boundaries to account for

The frontend still uses the existing browser-side token storage pattern. Browser storage is readable by same-origin JavaScript, so an XSS vulnerability could expose an active token despite server-side rotation and revocation. Before handling higher-risk data, consider migrating the frontend/backend to a same-site backend-for-frontend with `HttpOnly`, `Secure`, appropriately `SameSite` cookies and CSRF protection; that requires deliberate domain and CORS design and is not silently simulated in this change.

Rate limiting, bounded concurrency, and input validation help with common abuse but are not a substitute for an edge WAF/DDoS protection or a load test against the number of concurrent users you expect. The new concurrency test verifies atomic rate-limit behavior across 16 simultaneous database sessions; it is not a full capacity benchmark.
