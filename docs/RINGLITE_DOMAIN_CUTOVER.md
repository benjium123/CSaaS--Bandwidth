# Ringlite domain cutover

Target product name: **Ringlite**. Target public origin: **https://ringlite.io**.
The repository changes do not deploy the site, change DNS, or modify provider dashboards. The owner will deploy.

## Apply the release

Deploy the Ringlite commit from GitHub main using the existing deployment process. Rebuild the frontend; changing backend environment values alone does not update the browser title or wordmarks. Keep the current database, volumes, secrets, cookie names, and internal service identifiers. The public rename requires no database migration.

Apply the non-secret values from `deploy/ringlite.env.example` to the existing production `.env`; do not replace the entire file or erase its credentials. Restart/recreate the API and relevant workers so they read the new configuration.

The target values are:

| Setting | Target value |
| --- | --- |
| `APP_NAME` | `Ringlite` |
| `PUBLIC_WEB_URL` | `https://ringlite.io` |
| `PUBLIC_BASE_URL` | `https://ringlite.io` |
| `CORS_ORIGINS` | `https://ringlite.io` |
| `WEBAUTHN_RP_ID` | `ringlite.io` |
| `LIVEKIT_PUBLIC_URL` | `wss://ringlite.io/livekit` |

The passkey settings must follow the transition procedure below. Local development can retain localhost URLs. Keep the verified `SMTP_FROM` sender until a Ringlite sender domain has been configured and verified with the mail provider; changing an email address alone does not configure SPF, DKIM, or DMARC. Leave existing provider secrets unchanged.

## DNS, TLS, and proxy

Point the apex `ringlite.io` A record to the deployment server (currently `144.126.152.175`). Remove or correct any conflicting A/AAAA record. Only add `www` if that hostname will actually be served and included in its certificate. Public DNS was not resolving at preparation time; verify it again during deployment.

Use `deploy/ringlite-http.conf` for HTTP certificate validation, then obtain a valid certificate for `ringlite.io` before replacing that site configuration with `deploy/ringlite-https.conf`. The bootstrap serves ACME challenges only; normal requests return 503 until the TLS configuration is installed. Install the existing authentication rate-limit zone once in the Nginx HTTP context. Run `nginx -t` before reloading. The new site must preserve SPA fallback, API forwarding, event WebSocket upgrades, LiveKit WebSocket forwarding, and media/API upload limits.

Keep the old host and its certificate available through the transition. Do not redirect old webhook POST requests to a different host: provider retries and signature validation may not survive redirects. Both old and new webhook addresses should reach the same application while callbacks are migrated and in-flight events drain.

## Preserve admin access before changing passkey settings

Passkeys are bound to a relying-party domain. A passkey registered for `csaas.sabinepropertygroup.net` cannot simply be used for `ringlite.io`.

Before changing `PUBLIC_WEB_URL` or `WEBAUTHN_RP_ID`, sign in on the old host and enroll an authenticator app through `/admin/security`. Verify that each administrator has a usable alternate sign-in method and securely retained recovery codes where available. The existing `admin@apex-path.org` account had a passkey but no TOTP at the last check; verify its current state before cutover.

The backend uses one configured WebAuthn origin/RP. Leaving the old hostname reachable alone does not preserve old passkey authentication after those global settings change. After switching, sign in with the alternate factor and register a new passkey on `ringlite.io`. Verify privileged operations as well as sign-in; do not weaken the privileged passkey policy to make the migration appear successful. If access is blocked, restore the old public origin/RP configuration and complete the account transition before retrying.

## Provider dashboard callbacks

Change these dashboard destinations only after the new HTTPS site is reachable. These are external provider settings; deploying the repository does not update them automatically.

| Provider / purpose | New destination |
| --- | --- |
| **Didit KYC webhook** | **https://ringlite.io/api/v1/webhooks/didit** |
| Telnyx messaging profile webhook | `https://ringlite.io/api/v1/webhooks/telnyx/messaging` |
| Telnyx Call Control webhook, if used | `https://ringlite.io/api/v1/webhooks/telnyx/voice` |
| Stripe webhook, if configured | `https://ringlite.io/api/v1/webhooks/stripe` |
| LiveKit webhook, if configured | `https://ringlite.io/api/v1/webhooks/livekit` |

For Didit, edit the existing destination to the new URL at cutover. Preserve the selected workflow and configured events (`status.updated` and `data.updated`). Preserve the existing webhook signing secret when editing the destination; if the provider rotates it, update `DIDIT_WEBHOOK_SECRET` securely in production before testing. Do not put API keys or webhook secrets into documentation or Git. Didit's vendor API base URL stays unchanged. The browser return URL is separate from the webhook and follows the page origin for newly started verification sessions.

If enterprise SSO is configured, update the identity provider's OIDC redirect URI to `https://ringlite.io/auth/sso/callback`. SAML uses per-workspace metadata and ACS URLs under `https://ringlite.io/api/v1/auth/saml/{org_slug}/`; verify the workspace's metadata and update the identity provider accordingly. Old in-flight login/verification sessions and already-issued email links may retain the old host.

Optional carrier adapters keep their existing paths. If used, update `TWILIO_WEBHOOK_URL`, `PLIVO_WEBHOOK_URL`, and `SIGNALWIRE_WEBHOOK_URL` to their corresponding `/api/v1/webhooks/<carrier>/messaging` endpoint on the new domain, together with any provider dashboard setting. These exact URL values participate in signature validation. Do not enable unused adapters as part of the rename.

## Verify after deployment

- `https://ringlite.io/healthz` reports a healthy application and database, with a valid TLS certificate.
- Customer `/login` and `/signup` show Ringlite; `/admin/login`, invitation-based `/admin/signup`, `/admin`, and `/admin/security` remain separate admin entry points.
- Sign-in, MFA, invitation links, password reset links, and any configured SSO work on the new origin.
- A Didit test verification delivers a correctly signed event to the new webhook and updates the application record. An unsigned request is expected to be rejected; a generic HTTP 200 is not proof of working verification.
- Telnyx delivery/call events and a real authorized calling test work; LiveKit and event WebSockets connect using the new origin.
- Inspect errors and provider delivery history before retiring the old host. Keep a copy of the previous environment and proxy configuration for rollback.
