# Plan — the new-signup journey, presented as an ordered UI

Status: **plan only, nothing built.** Written 2026-09-18 after briefings from the session
that built the KYC backend (P41/P43) and the session that audited the auth (P42).

## What exists, and what doesn't

The backend is complete. `routes/kyc.py` covers business details, use case, persons,
residential addresses, ID+selfie via Stripe Identity, documents, the agreement, submit,
member self-verification, limit requests and step-ups. Behind it: sanctions screening, a
ban list, domain-age and email-domain checks, name matching, AI document reading, risk
scoring, operator decisions in `/ops`, and annual re-verification.

What does not exist is any **ordered journey through it**. `VerifyBusinessPage` sits in
Settings. The inbox's `OnboardingChecklist` lists four items — provider, number, team,
registration — and does not mention verification at all. A new customer has to find it.

## Three corrections that shaped this plan

Each of these would have produced a wrong build:

1. **The video call is gone.** D-P41-3 was superseded by D-P43-4; high risk is now handled
   by stricter automatic document matching, invisible to the customer. There is no
   scheduling to build, and building it would promise something the backend won't do.
2. **There is no canonical order in the backend.** Every section is an independent write
   against a `draft` profile, in any order, any number of times. The only gate is
   `missing_for_submission` at `POST /kyc/submit`. The ordered journey is therefore a **UI
   construct** — which is legitimate, but it means **no step may be rendered as locked**.
   Locking implies a backend rule that does not exist, and two tabs would disprove it.
3. **There is no public signup.** `allow_open_registration` defaults to False and is a
   fatal boot problem in production. A "new signup" is an **invited member joining a
   workspace**, or the operator creating the first one. The journey is designed for someone
   who already has an account and an org — not a stranger off the internet.

   These two are **not the same journey and must not share one stepper**: the first user of
   a brand-new workspace creates the org and owns the verification; an invited member never
   creates an org, so never sees that step, and if their role is privileged they can meet
   the passkey gate on their first authenticated request. Which of the two the operator
   actually runs is an open question below.

## The journey

**Step zero — a second factor.** Not part of KYC and not optional: `auth/deps.py` refuses
every non-exempt path with `two_factor_required` until the user has an authenticator app or
a passkey. No verification screen is reachable without it. `SecureAccountPage` already does
this and stays as-is.

Then, presented in this order but **enforced in none**:

| # | Step | Endpoint | Required |
|---|---|---|---|
| 1 | Business details | `PUT /kyc/profile/business` | country (from `supported_countries`, currently US + GB), legal name, entity type, registered address, business email |
| 2 | How you'll use it | `PUT /kyc/profile/use-case` | what you do, opt-in method, list source, monthly calls, monthly texts, destination countries |
| 3 | Owners | `POST /kyc/persons` + `PUT /kyc/persons/{id}/address` | ≥1 owner; residential address for every owner and beneficial owner |
| 4 | Prove identity | `POST /kyc/persons/{id}/verify` | ID + selfie per owner, via Stripe Identity (redirect out and back) |
| 5 | Proof of address | `POST /kyc/documents` `kind=proof_of_address` + `person_id` | one per owner |
| 6 | A business document | `POST /kyc/documents` any other kind | at least one |
| 7 | Agreement | `POST /kyc/agreement` | `accepted_version` must equal `current_version` |
| → | Submit | `POST /kyc/submit` | refuses with `kyc_incomplete` and a list |

**The stepper is driven by `missing`**, the server-computed list on the profile — never by
the UI's own idea of completeness. Every future rule change is then inherited for free.
`missing` is only populated in `draft` and `needs_info`; in every other status it is `[]`,
which is a fact about the payload and not evidence of completeness.

Step 2 deserves a sentence of copy explaining *why* we ask: the declared use case is what
the traffic-monitoring AI later judges every campaign against. A vague answer there
degrades detection for that account permanently.

ID checks are asynchronous (`pending → processing → verified | requires_input`), so the
applicant keeps working while Stripe runs.

## State → screen

All eight statuses. `suspended` and `reverification_due` are the two most likely to be
forgotten and the two where a wrong screen does real damage.

| Status | Can send/call? | Editable? | The screen | Must not |
|---|---|---|---|---|
| `draft` | No | Yes | The wizard. `missing` is the checklist; submit enabled only when it is empty | Lock steps; compute completeness client-side |
| `submitted` | No | No | What was sent, and that it is with a reviewer. `submitted_at` shown as a fact | Invent an SLA; animate a progress bar |
| `in_review` | No | No | Same as submitted, wording says being reviewed | Imply it is nearly done |
| `needs_info` | No | **Yes** | **`info_request` verbatim and prominent — it is the screen.** `missing` is live again; resubmit via the same endpoint | Summarise or reframe the operator's words |
| `approved` | **Yes** | **Use case only** | "You can send now, within these limits" — `limits`, `deposit_required_cents` when set. A use-case change is accepted and held as `use_case_pending` | Say "you're done"; offer edits to anything else — they get a tailored 409 |
| `rejected` | No | No | `decision_reason` verbatim, attributed as the reviewer's note. Terminal | Offer retry, appeal, or "start a new application" — no transition leaves `rejected`, and a ban may be attached |
| `suspended` | No | No | Suspended; contact route; "we've emailed the account owners with the details" | Claim a reason — the payload carries none for any member (see open questions) |
| `reverification_due` | **Yes** | **No** (use case only, as pending) | Re-verify prompt with `next_reverification_at`. Work continues | Block the workspace; offer resubmit; show a re-check button to the wrong person |

Sources: `KYC_TELEPHONY_STATUSES = {approved, reverification_due}`,
`KYC_EDITABLE_STATUSES = {draft, needs_info}` (`models/kyc.py:40` — that is the whole set),
`ALLOWED_TRANSITIONS["rejected"] = ∅`, and `_profile_out` gating `info_request` to
`needs_info` and `decision_reason` to `rejected`.

### Two rows that are subtler than they look

**`reverification_due` is NOT editable.** An earlier draft of this table said it was, and it
is wrong: business details, persons, addresses and documents all pass `_require_editable`
and 409 with "This application can no longer be edited". What *is* permitted is the one
thing the screen needs — **re-running the ID check**, via an explicit exception in
`start_person_verification` for an already-verified person when the profile is in
`reverification_due` or `needs_info`.

But **only that person can run it**. If the `KycPerson` has a `user_id`, any other actor
gets `not_your_identity` — "Only {full_name} can repeat their own ID check". So the screen
has two shapes: a button for the owner who must act, and for everyone else a line naming
*which* owner we are waiting on, with **no button**. Otherwise an admin clicks it and
collects a 403 they can do nothing about. That distinction is the difference between "the
workspace is blocked" and "we're waiting on Sam".

There is also **no resubmit** here: `submitted` is not in
`ALLOWED_TRANSITIONS["reverification_due"]`, so only an operator moves it on.

**`approved` is editable in exactly one respect.** `set_use_case` branches: in
`draft`/`needs_info` it applies immediately; in `approved`/`reverification_due` it writes
`use_case_pending` and returns `"pending_review"` rather than refusing. That is why
`use_case_pending` is in the payload — it is the only post-approval self-service change, and
it is a real screen: a verified business telling us it now does something different. Surface
it as "submitted, awaiting review" while set, and expect a step-up, since `use_case_change`
is already in `ACTION_LABELS`. Every other edit in `approved` returns the tailored refusal
"Your business is approved; contact support to change these details" — render that verbatim
rather than inventing our own.

## Where each step's truth lives

A stepper is a UI that asserts where someone is in a process, so every step names the
server resource that owns its state and what it renders before that resource answers.
Nothing is derived from a second source, and nothing renders a claim while loading.

| Step | Truth lives in | Ticked when | While in flight |
|---|---|---|---|
| Second factor | `/auth/me` | `totp_enabled \|\| has_passkey` | Nothing — no tick, no cross |
| Workspace | `/auth/me` `memberships` | non-empty (an empty array from a *loaded* response is real information) | Nothing |
| Every KYC step | `GET /kyc/profile` | absent from `missing` — **only meaningful in `draft`/`needs_info`; the stepper is not mounted in any other status** | Skeleton; no step state at all |
| Member ID verification | `GET /kyc/profile` persons — **but see below; it is not an onboarding step** | all four conditions below | Nothing |

**Member ID verification is a post-approval step for privileged non-owners, and does not
belong in the signup wizard.** `_require_verified_privileged_member` (deps.py:410-438)
returns early — no requirement at all — in three cases before it ever looks at whether the
member is verified:

1. `not settings.kyc_enforced`, or no membership. Same shape as the 2FA trap: with the flag
   off nobody is gated, and a step rendered as required is a red cross on a user with
   nothing wrong.
2. **The member is an owner** (`WILDCARD_PERMISSION in role.permissions`) — "owners are the
   verified people on the application itself". For an owner this step must be **absent**,
   not ticked and not pending: there is no `KycPerson` row for them to get verified, so a
   step would sit permanently incomplete with nothing that could clear it.
3. **The org's KYC status is not in `{approved, reverification_due}`** — "before approval
   the application itself is the gate". That covers `draft`, `submitted`, `in_review` and
   `needs_info`, which is **the entire duration of onboarding**. Listing it as a step during
   draft invents a requirement the server does not have, and blocking progress on it blocks
   on something the server would let through.

So the real condition is
`kyc_enforced && !isOwner && status ∈ {approved, reverification_due} && thatPerson.status !== "verified"`,
it is per-member, and it only gates `IDENTITY_GATED_PERMISSIONS` (billing, member
management, roles) — never the workspace. It surfaces after approval, via the existing
step-up dialog, not in this journey.

### The rule this plan was wrong about three times

**A policy switch is not a fact about a person.** Three separate steps in earlier drafts
read a flag that means "this rule is currently enforced" as if it meant "this person has
done the thing":

| Field | Means | Was read as |
|---|---|---|
| `second_factor_required` | the 2FA policy is on AND they lack a factor | they have a factor |
| `passkey_required` | the grace clock has started | they are blocked now |
| (kyc_enforced / owner / pre-approval early returns) | this gate does not apply | this person is incomplete |

What makes this worse than the in-flight problem the rest of this table guards against is
that it is **stable**. A value that is wrong because it has not loaded is wrong for 200ms
and then right, and a loading discipline catches it. A policy flag read as a fact is wrong
forever, and every reload confirms it. All three instances would have shipped past a
correct loading rule.

The severity is not uniform, either. A wrong tick is cosmetic. But the owner case —
condition 2 below — is a **blocking step, shown to the account owner, with no mechanism
anywhere in the product that could ever satisfy it**. That is a support ticket that cannot
be resolved by doing anything, shown to the one person who cannot be told to ask their
admin.

**The stepper is mounted only in `draft` and `needs_info`.** `missing` is `[]` in every
other status, so a stepper rendered anywhere else ticks every step green vacuously — and in
`rejected` that is a fully-completed checklist sitting beside a rejection. This is the same
shape as the rule above: an absence (`[]`) read as an achievement (done). The fact is
recorded in the state table too, but this is the row someone will implement from.

**The 2FA tick is derived from the fact, never from the policy.** This is the trap the auth
session bet on, and it is the opposite of the one I was braced for:
`second_factor_required` is `require_2fa_all_users AND not has_second_factor`. In a
deployment with `REQUIRE_2FA_ALL_USERS=false` that field is `false` for someone with
**nothing enrolled at all** — indistinguishable from someone who finished the step. A tick
driven off `!second_factor_required` shows a green check to a user who has no second
factor. So: `totp_enabled || has_passkey` decides the **tick**; `second_factor_required`
decides only whether the step **blocks**.

## The three auth gates, and what they actually mean

Not one ordered pipeline — two blanket per-request gates and one per-permission.

1. **Platform 2FA** (`deps.py:148`) — `require_2fa_all_users` (default true) and no second
   factor. Exempt: `/api/v1/auth/`, `/api/v1/me/sessions`, `/api/v1/me/login-events`.
   Fires inside `get_current_user`, **before any org exists**, and `/api/v1/orgs` is not
   exempt — so a new owner genuinely cannot create a workspace first. Confirmed as the
   contract, not routing luck. But it is conditional on the flag: where the flag is off,
   the step must not render as blocking.
2. **Org 2FA** (`deps.py:349`) — the per-workspace P25 policy. Same error code, distinct
   gate, can fire when the platform one does not.
3. **Passkey** (`passkey_policy.enforce`) — privileged roles only. **`passkey_required` on
   `/auth/me` means "the grace clock has started", not "blocked now"**: the first blocked
   request stamps `passkey_required_since` and commits. Paired with `passkey_grace_until`
   to decide whether it blocks. A stepper that halts on the boolean walls in every admin on
   day one for a policy that is deliberately not yet enforcing.

**`identity_verification_required` is a standing state, not a step-up**, and it is **not on
`/auth/me` at all**. It fires only inside `require_permission` for
`IDENTITY_GATED_PERMISSIONS`, only when KYC is enforced, the caller is not an owner, the
org is approved, and that member has no verified `KycPerson`. Today the only ways to know
are to take the 403 or to read the KYC profile. **It must not be inferred from
`permissions`** — the permission is present in the role and the gate fires anyway, so the
inference says "allowed" for someone who will be refused. Until a field exists, that step
renders from the KYC profile, never from `/auth/me`.

## What the customer may be shown

The allowlist is the contract: `CUSTOMER_VISIBLE_CHECKS = (registry, website, email_domain,
name_match, documents)`, and only `{result, summary}` of each.

Never rendered, and not to be added: **sanctions**, **ban_list**, the **AI decision pack**,
and **`risk_tier` / `risk_reasons`** — the last two are not in `_profile_out` at all. If a
design ever wants a "your application is high risk" state, that instinct is the bug.

Check summaries are prose written for operators and may contain a registry's own error
text. They are rendered as a quoted fact with our own action beside them, never folded into
a sentence of ours whose meaning would change if the summary is blunt.

**The allowlist is not the whole disclosure surface.** Documents carry a separate
per-document `review_message`, and that is where a real leak lived until `be5fe72`: the
forgery detector's own reason, "The document shows signs of editing", was being handed to
the applicant it fired on — upload, learn it was spotted, adjust, re-upload, in minutes and
for free. It now reads as a request for the original document, which is actionable for an
honest applicant with a bad phone scan and indistinguishable to a forger from what we would
say about any poor copy. Only that one reason is rewritten; the others (a stale bill, a
mismatched address) must stay verbatim, because nobody can fix those without being told.

**The line not to cross:** the `documents` check summary says *that* something does not
match, never *why*; the AI's actual reasons live in `check.detail`, which `_profile_out`
strips. A future ticket asking to "tell them what's wrong with the document" is asking to
reopen this, and should be refused on those grounds.

## The AI, stated precisely

- **During onboarding the AI never decides.** It reads documents and writes an
  operator-facing pack with a recommendation; approval is a human click and
  `approval_blockers` are enforced regardless. The customer sees one downstream artefact:
  the `documents` check result. **No UI copy may imply an automated verdict.**
- **After approval the AI can act** — under `MONITOR_ENFORCED` it can hold texts and pause
  an account (banning stays human). The customer-facing state is the `account_paused`
  refusal with the server's own message. That belongs to the running console, not to this
  journey. Two different promises; the copy must not blur them.

## Two entry points, not one

1. **The workspace journey** — above. Owner-driven, once per business.
2. **A privileged member verifying themselves** (D-P41-8) — admin and billing members prove
   their own ID on an **already-approved** business. This is **not a journey and not part of
   the wizard**: it cannot fire before approval (condition 3 above), never applies to
   owners, and gates only billing and member-management powers. The API refuses the action
   with `identity_verification_required`, and the existing dialog handles the redirect.
   What needs building is the **return page** from Stripe, which must show `processing`
   honestly because Stripe is frequently not finished when the customer lands back.

## What this deliberately does not smooth over

- Waiting is the product working. No invented SLA, no motion implying progress.
- Rejection is terminal and may carry a ban; a friendly "try again" would quietly attempt
  to undo it.
- Limits and deposits exist to make abuse expensive and apply **after** approval.

## Open questions for the operator

1. **There is no email on any KYC decision.** `approve`, `reject` and `request_info` change
   status and write an audit row and send nothing. Today a customer learns their outcome
   only by returning to the app and looking — a banner plus polling is the entire
   notification mechanism. Suspension *does* email. The KYC backend session has offered to
   build decision emails but will not add customer-facing behaviour unilaterally.
2. **`suspended` carries no reason in the customer payload — and the question is audience,
   not secrecy.** `decision_reason` is gated to `rejected` and `info_request` to
   `needs_info`, so the in-app screen can say only that the account is suspended. But
   `suspension.py:167` already emails the owners a message containing `Reason: {reason}`.
   So the reason is not withheld; it is delivered to a different audience. The suspension
   email goes to **owners only**, while `GET /kyc/profile` sits behind `org:read`, which is
   **every member** — potentially including the person the compliance concern is about.
   `decision_reason` is not a precedent for mirroring it, because a rejected org is
   pre-approval with a couple of people in it and a suspended org is a live business with
   staff. The backend session's recommendation, which I'd endorse: surface it in-app
   **gated to owners**, which needs a permission-scoped field in `_profile_out`. Until
   then the screen says suspended, gives the contact route, claims no reason, and adds one
   true line — "we've emailed the account owners with the details" — so a staff member
   knows where to look instead of hitting a dead end.
3. **The `website` check summary states our threshold** — "the domain is only 12 days old"
   when under 90. Domain age is public RDAP data, but the sentence teaches an applicant
   that domain age is scored. The backend session has offered to reword it to state the
   fact without the threshold.

4. **Which entry point do you actually run** — invite-only, or operator-provisioned first
   workspaces, or both? The two hit different gates and need different first screens.
5. **A privileged member has no way to know they need to verify** except by being refused.
   `/auth/me` carries no field for it. If one is added, it must **not be a boolean**:

   ```
   identity_verification: "not_applicable" | "required" | "verified"
   ```

   - `not_applicable` — `kyc_enforced` off, the caller is an owner, or the org's status is
     not in `{approved, reverification_due}`. Render nothing.
   - `required` — the gate applies and this member has no verified `KycPerson`. Render the
     step; those powers are blocked.
   - `verified` — the gate applies and they have cleared it. Render the tick.

   **A boolean here would be the policy-switch trap one level up**, and this plan has
   already been wrong about that three times: `false` would mean both "done" and "doesn't
   apply to you" — a policy switch and a fact about a person sharing one field. The
   three-state value is not extra scope; it is the minimum shape that can carry the answer.

   Two constraints for whoever builds it, both from the auth session and both endorsed
   here:
   - It must be **computed by the same function the gate calls**, not re-derived in the
     route, or the payload and the gate disagree the first time someone edits either. Every
     input already lives inside `_require_verified_privileged_member`; a boolean discards
     three of them at the boundary and asks the client to reconstruct them — which is how
     the second implementation gets born after all.
   - **`not_applicable` must be computed, never a default for "we could not tell."** An
     unknown must be absent or an error. A fallback of `not_applicable` renders nothing and
     silently hides a step that was required — reintroducing, as a default, the exact
     failure this field exists to prevent.

## Ownership

Frontend is mine; the backend session keeps `backend/`. Items 1 and 3 above are theirs to
build if the operator agrees. Both sessions review the state table before implementation.
