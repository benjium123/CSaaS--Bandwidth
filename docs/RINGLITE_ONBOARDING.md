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
