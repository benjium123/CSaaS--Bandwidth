# Trust & safety features

What exists today to keep scammers and account takeovers off the platform. One line each.

## Signup & business verification
- Invite-only signup; no public registration.
- Businesses only, registered in the US, Canada or UK.
- Business details, registration number, tax ID, address, website and business email collected.
- Business documents uploaded, file type checked, encrypted at rest, viewable only by operators.
- Every owner (25%+) passes an ID + live selfie check (Stripe Identity); we never store ID images.
- Declared use case: what they call/text for, who they contact, where numbers come from, volumes, countries.
- Signed agreement: truthful info, consent laws, monitoring for abuse, per-violation penalty, no reselling.
- Automatic checks on submit: registry (UK automatic, US/CA manual), sanctions lists (US/UK/Canada), ban list, website and domain age, email domain, ID names vs. declared names.
- AI reviewer summary of each application (advisory only).
- Risk tier with plain-language reasons; high risk requires a video call before approval.
- An operator approves or rejects every business; approval is blocked until all checks are clear.
- One unverified workspace per person at a time.
- Annual re-verification of owners, with a 14-day grace period.
- Daily sanctions re-screen of approved businesses.

## Login & account security
- Two-factor authentication required for every account (authenticator app or passkey, never SMS).
- Passkeys supported for sign-in.
- Risky sign-ins flagged (outside US/CA/UK, VPN/Tor/hosting network, new device, new country): extra check, owner email, operator alert.
- Fresh passkey/authenticator check before sensitive operator actions.
- Fresh ID + selfie before: changing payment method, requesting higher limits, bulk number orders, creating API keys, granting admin/billing/owner, changing the declared use case.
- Admin and billing members of an approved business verify their own ID before using those powers.
- Owners, admins, billing and operators must sign in with a passkey (14-day grace).
- Session list, remote sign-out, login history.
- Per-workspace IP allowlist (existing).

## Passwords, sessions & recovery
- Passwords: 12+ characters; ones leaked in known breaches are refused.
- Forgot/reset password by email; a reset never skips the second factor.
- Account locks after repeated wrong passwords or codes; owner emailed; operator unlock.
- One-time recovery codes; using one flags the sign-in and alerts.
- Lost every factor: ID + selfie that must match the verified person, then a 24-hour wait before sensitive actions.
- Sessions in secure browser cookies; signed out after 30 minutes idle or 12 hours total (workspaces can be stricter).
- Password change, removal from a workspace or a role change ends that person's sessions.
- Personal account activity log (password, factors, recovery, lockouts).
- Shared rate limits on sign-in, reset, recovery and SSO; the client IP can't be faked.

## Enterprise sign-in
- Single sign-on with OpenID Connect or SAML, only for email domains the workspace proved it owns (DNS).
- SAML accepts only signed, unexpired, single-use answers to a sign-in we started.
- SSO sign-ins go through the same risk checks as passwords.
- SCIM user sync: people removed in the company's identity provider lose access here at once.
- API keys: optional IP ranges, last-used IP, 1-year maximum life, overlap on rotation.

## Limits & calling/texting gate
- No texting, calling or phone numbers until the business is approved.
- Optional per-business deposit, daily call limit, daily text limit and number cap.
- Prepaid credit hard gate for texts, calls and number rental (existing).
- Opt-out keywords, quiet hours, do-not-call list and 10DLC registration checks (existing).
- Call recording consent announcements (existing).

## Suspension & ban list
- One-click suspension: ends sessions, revokes API keys, cancels scheduled texts, pauses campaigns, hangs up live calls, emails owners.
- Platform ban list (hashed): company numbers, tax IDs, emails, domains, phones, addresses, card fingerprints, devices, verified people.
- Rejected or suspended businesses can be banned in one step; returning applicants are matched automatically.
- Cards on the ban list are refused when added.

## Operator tools
- Named operator accounts (reviewer / admin) with required second factor; no shared password for reviews.
- Operator console: review queue, application detail, documents, checks, AI summary, decisions, limits, suspension.
- Security alerts queue (flagged logins, limit requests, sanctions hits).
- Every decision, document view and suspension is written to the audit log with the operator's name.
- Operator unlock, 2FA reset, deactivate and reactivate for any user, each audited.
- Audit log also covers number purchases/releases, invites, SSO/SCIM provisioning and domain changes.
- Decision emails to business owners (approved, more info needed, rejected).
