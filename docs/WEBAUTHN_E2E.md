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
- No **physical** device was ever involved, and no real credential was created. The keys
  lived in page memory. A page reload once lost the override and let a real
  `navigator.credentials.get` reach the operator's actual hardware — see *Hazards* below.

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

## The concurrent claim — narrowed, not closed

The single-use challenge was first proven only **sequentially**, and both sessions assumed
the concurrent case needed two browsers. It does not: `consume_challenge` runs **before**
verification, so the claim can be raced with a credential that is merely well-formed. No
authenticator, no valid crypto, no prompt.

One challenge minted, then two simultaneous `POST /passkeys/login/verify` with the same
`challenge_id`. Over four attempts, **exactly one request got past the claim every time**:

| Outcome | Message |
|---|---|
| Winner (claimed the challenge, then failed later) | 401 "That passkey is not registered to this account" |
| Loser (refused at the claim) | 401 "This passkey request has expired - try again" |

Never two winners. Note the winner fails at **credential lookup**, not signature
verification — the claim happens before the credential is resolved, so the predicted 422 is
actually a 401 with a different message. The property held; the status pair we expected did
not, which is worth knowing before someone writes an assertion against it.

**This narrows the gap, it does not close it.** SQLite serialises writers, so passing here
is necessary but weaker than PostgreSQL, where the conditional UPDATE's row-locking remains
reasoning rather than observation. What it does settle is the claim that *no test issues two
simultaneous requests against a challenge* — one now does.

## Corroborated in the database, not just by status codes

The audit session queried the resulting SQLite file read-only. Three things HTTP could not
show:

- **8 challenges, 1 unconsumed.** Cross-referenced with `login_events` (5 ok, 2 bad_2fa),
  every login challenge carries `consumed_at` — including the two that belonged to *failed*
  ceremonies. The burn-on-failure rule observed as rows.
- **`sign_count` stayed at its high-water mark of 4** after the rolled-back assertion. The
  server refused it *and* declined to persist the lower value. A fix that returned 401 while
  still writing the rollback would look identical from the browser and would silently
  disable clone detection from then on.
- **`auth_method='passkey'` and `second_factor_at` were set on all four passkey sessions.**
  Neither column had ever been checked end to end, and both are load-bearing:
  `passkey_policy.session_satisfies` tests `auth_method`, and `check_step_up("recent_2fa")`
  reads `second_factor_at`. So a passkey sign-in demonstrably satisfies the privileged-role
  passkey requirement *and* counts as a fresh second factor — a stronger result than the one
  being aimed at.

## Hazards for whoever runs this next

- **Deliberate failures feed the real lockout counter.** `bad_2fa` is in `FAILURE_OUTCOMES`,
  threshold 10 per 15 minutes. The fourth concurrency run hit it: `423 "Too many failed
  attempts. Try again in 15 minutes or reset your password."` That is the lockout working,
  but budget for it or reset the account between runs, or the next person debugs a 423
  instead of their test.
- **A page reload silently disarms a software authenticator.** The override lives in page
  memory; after a reload, `navigator.credentials` is the real one again, and the next call
  prompts the **operator's own devices** — in this run it reached for their iPhone over the
  cross-device flow. Install a guard that throws when the override is missing rather than
  letting it fall through to real hardware. Nothing was ever written to a real keychain, but
  the prompt itself is intrusive and should not happen twice.

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
