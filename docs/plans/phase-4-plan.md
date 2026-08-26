# Phase 4 — Numbers, 10DLC, and toll-free verification

## Goal

Own the full life of a number: find it, order it, register it for the traffic it will
actually carry, and refuse to send from it until that registration is real.

## The invariant this phase exists to create

Today an unregistered number is detected by a **carrier rejection** — Bandwidth `4476`,
Telnyx `40300`. That is the worst possible place to find out. By then we have:

- burned an API call and a carrier-side violation,
- earned a reputation ding on a brand that takes weeks to rebuild,
- and produced a `rejected` message the operator has to reconcile by hand.

> **DR-1 — Registration is a PRE-SEND gate, not a post-send error.**
> A number whose campaign is not `approved` cannot be selected by the router and cannot be
> sent from. The carrier rejection code stays as a tripwire, but it should never fire again
> — if it does, our state disagrees with the carrier's and that is itself the alarm.

This inverts the current relationship: the carrier stops being the thing that tells us we
are non-compliant, and becomes the thing that confirms we already knew.

## DR-2 — Provisioning is a SEPARATE protocol from messaging

`MessagingCarrier` stays exactly as it is. Number provisioning gets its own
`NumberProvider` protocol, because the two capabilities genuinely come apart: the trial
Bandwidth account can order numbers but not message, and a carrier may be usable for
sending long before we let it provision. Squashing both into one interface would force
every adapter to implement methods it cannot honour and would push the failure to runtime.

Capability is **declared**, discovered by asking the registry, and never probed by trying.

## DR-3 — Brand and campaign are OUR entities with per-carrier external ids

10DLC ultimately lands in TCR, but every carrier fronts it with a different API and its own
ids. Modelling `Brand`/`Campaign` locally with a `carrier_refs` map means:

- one brand can be registered with two carriers (which is exactly the user's situation),
- the registration state machine is ours and is testable without a carrier,
- and a number's eligibility is a local join, not a network call in the send path.

A campaign is `draft → submitted → approved | rejected`. **Only `approved` unlocks sending**,
and the transition is monotonic in the same way message status is — a late webhook cannot
walk an approved campaign backwards into `submitted`.

## DR-4 — Toll-free verification is a different regime, modelled separately

TFV is not 10DLC: no brand, no campaign, no TCR — a submission with a use case that is
approved or rejected by the carrier. Modelling it as "a campaign with a flag" would put two
unrelated state machines in one table and guarantee a wrong `approved` somewhere. Separate
table, same pre-send gate.

## DR-5 — Releasing a number is soft, and blocked while it has history

A released number is `status='released'`, never deleted: threads, messages and the consent
ledger all reference it, and the ledger in particular is the evidence that someone opted
out. Hard-deleting a number would orphan the audit trail that exists to prove compliance.

## Allowed files

- `app/providers/numbers.py` (new — the `NumberProvider` protocol)
- `app/providers/{bandwidth,telnyx}/numbers.py` (new)
- `app/models/numbers.py` (new: Brand, Campaign, TollFreeVerification) + `org_numbers` extension
- `app/services/provisioning.py`, `app/services/registration.py` (new)
- `app/api/routes/numbers.py` (extend), `app/api/routes/registration.py` (new)
- `app/routing/router.py` — eligibility filter only
- `app/compliance/gate.py` — the pre-send registration check
- one additive migration
- tests

## Forbidden

- deleting an `org_numbers` row for any reason
- any path that lets a `draft` or `submitted` campaign send
- widening `MessagingCarrier` with provisioning methods
- editing P1/P2/P3/P3b seam tests

## Test spec

Unit:
- [ ] campaign status is monotonic; a late `submitted` webhook cannot undo `approved`
- [ ] a number with no campaign is ineligible; with an approved campaign, eligible
- [ ] toll-free numbers gate on TFV, not on 10DLC, and vice versa
- [ ] `NumberProvider` capability is declared, and asking a non-provider is a clean error

Integration:
- [ ] sending from an unregistered number is refused **before** the carrier is called
- [ ] the router skips ineligible numbers when choosing a sender
- [ ] ordering a number records it with the carrier that sold it
- [ ] releasing a number keeps the row, its threads and its consent history
- [ ] a TFV approval webhook flips a toll-free number to sendable

Pass criteria: all green, ruff clean over `backend/`, OpenAPI regenerated.

## Deploy

Blocked on R1 (Bandwidth credentials 401). Everything here is built and tested against
fixtures; the live gate is "order a number from the console and send on it", which is the
first thing to run once credentials work.
