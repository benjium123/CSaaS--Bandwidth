# WebAuthn end-to-end — what has actually been executed

Run 2026-09-18 against a real browser and a real server. Until this, every passkey path in
the console was covered only by stubs: jsdom has no WebAuthn, so the browser half was
unexercised **by construction**, and the joint statement listed it as an open gap.

## What was used, stated precisely

A **software authenticator installed in the page**: real ECDSA P-256 keys from WebCrypto,
real ECDSA-SHA256 signatures in DER, real CBOR attestation objects, real `clientDataJSON`,
verified by the real `py_webauthn` on the real backend. `navigator.credentials.create/get`
were replaced; everything on either side of them is production code. In particular the
console's own `src/lib/webauthn.ts` was imported and driven directly — the base64url
conversions, `toCreationOptions`/`toRequestOptions` and `serializeCredential` that are the
most likely place for a client bug are all exercised, not reimplemented.

Backend: `app.main` on SQLite, `AUTH_BEARER_COMPAT=false` so the session is the HttpOnly
cookie only, which is the production path.

**What this does NOT cover, and must not be read as covering:**
- No physical authenticator. Attestation formats from real keys, transports, and platform
  authenticator behaviour are untested.
- Chrome only. Safari/iOS is a different WebAuthn implementation and the one most likely to
  diverge.
- The browser's own half of the ceremony — origin binding, RP ID checks, user-gesture
  requirements — is bypassed by replacing `navigator.credentials`, not exercised.
- The single-use challenge is proven **sequentially**. The concurrent case — two requests
  racing for the same challenge row, which is what the conditional UPDATE exists for —
  cannot be produced by driving one browser, and remains reasoning about row locking.

## Results

| # | Property | Result |
|---|---|---|
| 1 | Passkey registration through the console's own client code | **201**, credential stored |
| 2 | `userVerification` on the wire | `"required"`, as configured |
| 3 | Replaying a **spent** registration challenge | **401** "This passkey request has expired" |
| 4 | A tampered credential (wrong `clientDataJSON`) | **422** "That passkey could not be verified" |
| 5 | Retrying a challenge after a **failed** attempt | **401** expired — the failure burned it |
| 6 | Passkey login, full ceremony | **200**, `access_token: null` (cookie session) |
| 7 | Replaying a spent **login** challenge | **401** expired |
| 8 | Sign counter increments across successive assertions | 1 → 2 → 3, all accepted |
| 9 | Sign counter rolled **back** (clone signal) | **401** "Passkey verification failed" |
| 10 | Whole sign-in through the real UI, not fetch calls | Password step → second-factor step → passkey → signed in |

Result 5 is the one that matters most for the console: the UI is built on the rule that a
passkey ceremony is never retried in place, because the server burns the challenge on **any**
outcome. That was previously an instruction in a comment; it is now an executed fact. A UI
that resubmitted a spent `challenge_id` would show "this passkey request has expired" and
look like a broken authenticator.

Result 9 was not previously observed by anything, anywhere — it is the clone-detection
signal the single-use fix exists to protect.

Result 10 exercises the branch jsdom can never reach: in a real browser
`window.PublicKeyCredential` exists, so the passkey control renders **enabled** with no
"this browser cannot use passkeys" notice, which is the opposite of what the unit tests can
see.

## Found by running it

The membership-less account used for the test landed on the workspace picker, which told it
*"You belong to more than one."* The lede was unconditional. Fixed — it now describes what
the page does rather than asserting something about the account. Only running it catches
that class.

## Reproducing

The backend launcher is a copy of the P43 demo script, adapted, at the session scratchpad
path referenced by the `webauthn-backend` entry in `D:\VOIP\.claude\launch.json`. **It lives
in a session scratchpad and will not survive**; giving it a durable home in the repo is an
open decision for the operator, not something this change assumed.
