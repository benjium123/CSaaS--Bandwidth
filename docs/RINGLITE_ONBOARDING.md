# Ringlite signup, review and number checkout

New customer registrations must confirm email before accessing workspace APIs or identity verification. Existing users retain their access. Confirmation links expire after 24 hours, are single-use, and only their hashes are stored. Resends are rate-limited. Configure either `RESEND_API_KEY` plus `RESEND_FROM` (a verified sender), or the existing SMTP settings. Failed delivery is shown to the customer and can be retried.

Platform admins can explicitly override automated KYC blockers using the review console. Reviewers retain normal restrictions. The audit records the administrator, decision note and overridden warnings. ID evidence is fetched from Didit's v3 session decision API, scoped to the selected application and person. Evidence access is audited, responses are non-cacheable, and media previews are served through authenticated endpoints without forwarding the Didit API key to storage hosts.

## Payment configuration

- `STRIPE_SECRET_KEY`: live Stripe secret key, configured only on the server.
- `STRIPE_WEBHOOK_SECRET`: signing secret for `/api/v1/webhooks/stripe`.
- `STRIPE_NUMBER_PRICE_ID`: `price_1UIESILz6FHVmZlMqHZPBBiw`.
- `PUBLIC_WEB_URL`: the deployed customer-facing origin; used for email and checkout return links.
- Telnyx credentials must support number search and ordering.

The current webhook URL is `https://csaas.sabinepropertygroup.net/api/v1/webhooks/stripe`. When the public site moves, use `https://ringlite.io/api/v1/webhooks/stripe` and update `PUBLIC_WEB_URL`. Subscribe to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, and `customer.subscription.created`, `.updated`, `.deleted`.

Checkout validates that the configured price is active, USD 15.00, monthly, and quantity-based before creating a subscription. The quantity equals the selected phone numbers. Each additional cart creates its own subscription; no separate seat charge is added. Existing traffic-credit billing remains separate. Stripe-funded numbers are excluded from prepaid rental charges.

Verified payment triggers Telnyx provisioning from either the signed webhook or the authenticated return-page request. A durable claim prevents concurrent requests from ordering twice. Each accepted number and inbox are persisted together. Pending carrier orders use the existing order poller. Releasing a purchased number reduces its subscription quantity; releasing the last number cancels that subscription, without refunding the elapsed billing period.

## Provisioning recovery

An ambiguous carrier failure is never retried blindly. The paid cart remains visible under **Administration → Billing → Phone number purchases**, and a security alert identifies the purchase. Reconcile the Telnyx order before retrying externally or issuing a refund; subscription links open Stripe for billing adjustments. A crash during the durable provisioning claim leaves the cart in `provisioning`, also visible in that list. Do not start a replacement paid checkout for that cart.

Production email delivery and live checkout require the above credentials. A price ID alone does not authorize Stripe API calls. The app never pretends payment succeeded or provisions numbers from a browser-supplied success flag.

## Telnyx confirmation emails
Set `TELNYX_EMAIL_FROM` to a sender on a Telnyx-verified domain. This selects Telnyx for transactional emails using the existing `TELNYX_API_KEY` and `/v2/email_messages`. Shared domain sending is restricted to the Telnyx account owner and cannot serve customer confirmations. Verify `mail.ringlite.io` and use `no-reply@mail.ringlite.io` in production. Confirmation links follow `PUBLIC_WEB_URL`. Provider failure remains visible and users can resend. API acceptance does not prove inbox delivery.

## Account administration
The All accounts tab lists every login, including unconfirmed signups and every application status. Named admins can manage customer accounts; operator accounts are protected. Permanent deletion requires recent 2FA, a reason, and the exact target email. Sole-member workspaces and their tenant data/uploads are deleted with the login. Shared-workspace owners must transfer ownership or remove other members first. Live numbers, subscriptions and unfinished orders must be closed before deletion to avoid orphaning billable resources. Provider-held data and backups remain subject to their own retention.

Email, submitted phone numbers and Didit verified identity hashes can be selected for blacklisting, independently or during deletion. Identity matching uses verified name/date-of-birth hashes, not biometric face matching or a raw document-number match. Blacklisting disables the current login and revokes sessions. Email bans are checked at registration; phones at verification save; all identity matches at application submission. A named super admin may still explicitly override review warnings through the existing manual approval flow. Hashed bans and a global operator audit survive deletion; the existing Ban list controls can deactivate bans.

## Company verification layout
Company onboarding uses one standalone flow: company details, representative/Didit, shared use case, ownership/residential addresses, company documents, then one final agreement and submit. The representative form collects only personal name, country and phone. Industry, business description, purpose and customer country live in the company use case. Version 4 applicant details do not require duplicate personal use-case answers or a separate agreement. Existing answers remain available and are used to prefill the shared fields.

Telnyx inventory search uses exact requested area codes. Provider code 10031 with a no-numbers message is shown as an empty result, and region-information arrays supply city/state labels. The paid checkout still requires STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET; a price ID alone cannot enable charging or verified provisioning.


## Unified signup and messaging
New self-serve signups accept personal or work email and always create a personal identity-verification workspace. There is no company/individual selector or company KYC requirement for new signups. Email confirmation, Didit and administrator approval remain mandatory before purchasing numbers and calling. The stored `individual` value is retained for compatibility; it no longer means voice-only. Historical company KYC data remains intact.

All account types can use existing messaging registration forms. Local Telnyx numbers require a company brand, carrier-approved 10DLC campaign, matching number assignment and fresh carrier approval evidence before SMS/MMS can leave the system. Toll-free numbers retain their separate carrier verification process. Registration is rechecked at dispatch, including queued messages. Identity approval alone never approves a campaign. Existing admin-imposed limits and suspensions remain enforced.
