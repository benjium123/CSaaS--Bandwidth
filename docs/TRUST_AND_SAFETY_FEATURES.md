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
- Session list, remote sign-out, login history.
- Per-workspace IP allowlist and single sign-on (existing).

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
- Decision emails to business owners (approved, more info needed, rejected).
