# Phase 3b — Carrier routing fabric

## Why this phase exists, and why it is here and not at P14

`PHASES.md` originally put multi-carrier failover in **P14**. That was wrong, and the
cost of being wrong compounds every phase. P1's `build_carrier(settings)` returns *one*
carrier and stashes it at `app.state.carrier`; every phase after this one — voice (P5),
streaming (P7), the dialer (P11) — would be written against that singular assumption and
would all need unpicking at P14.

The CAL surface is at its smallest right now: messaging only, voice not yet built. Moving
the fabric to P3b means voice is born multi-carrier instead of being retrofitted.

## The constraint that shapes everything

**A DID belongs to exactly one carrier. You cannot send from a Bandwidth number through
Telnyx.** Numbers are provisioned, 10DLC-registered and STIR/SHAKEN-attested at a specific
carrier. `OrgNumber.carrier` already records this.

The consequence is the single most important thing in this document:

> Routing does not choose a carrier. It chooses a **(number, carrier) pair** — and
> choosing a different carrier means the recipient sees a **different sender**.

So "failover to Telnyx when Bandwidth is down" is not a transparent operation. It is a
visible change of identity mid-conversation. Any design that pretends otherwise produces
threads where a recipient is answered by a stranger.

### DR-1 — Failover is intra-carrier by default; cross-carrier is opt-in and never mid-thread

- **Intra-carrier**: another healthy number on the same carrier. Transparent-ish, same
  brand registration, same attestation. Always allowed.
- **Cross-carrier**: a different carrier, therefore a different number. Allowed only when
  the org opts in **and** the message is new outreach, never a reply in an existing thread.

A thread that has been spoken to keeps its sender or it does not get sent. Silence is
recoverable; a confused recipient replying STOP to a number they don't recognise is not.

This is affordable only because the consent ledger is keyed `(org_id, contact_e164, channel)`
with **no `our_e164`** (P3, D7). Suppression is pool-wide, so a carrier switch cannot leak a
message past someone's STOP. Had consent been per-number, cross-carrier failover would be a
compliance hazard and would be forbidden outright.

### DR-2 — Explicit beats clever, always

Precedence, highest first:

1. an explicit `from` on the request — pins the carrier that owns that number
2. an explicit `carrier` on the request — "at will", the operator override
3. the thread's sticky sender (P2) — continuity wins over optimisation
4. the org's routing policy — ordered preference, health-aware
5. the org's default number

Health *reorders* candidates within the policy. It never overrides levels 1–3. An operator
who names a carrier gets that carrier or a clear error — never a silent substitution,
because a silent substitution is how you discover at 2am that half your traffic left on the
wrong brand.

### DR-3 — Health is a decaying in-process circuit breaker, not a database table

Carrier health changes on a timescale of seconds and is per-process observable. Writing it
to Postgres would add a write to every send path and still be stale. Three states —
`closed` → `open` → `half_open` — driven by consecutive *retryable* failures. Auth failures
and `invalid_request` never open the breaker: those are our bugs, not the carrier's outage,
and retrying them elsewhere just spreads the bug.

### DR-4 — The registry replaces "the carrier"

`app.state.carrier` (singular) becomes `app.state.carriers`, a `CarrierRegistry` built once
at boot holding every configured adapter. `build_carrier` stays as a thin
`registry.primary()` shim so P1/P2 seam tests keep passing unmodified — the seam tests are
the evidence the abstraction held, and editing them to fit a new design would be editing
the evidence.

### DR-5 — SignalWire rides the Twilio-compatible API

SignalWire's Compatibility API is a Twilio clone. Writing it as a Twilio-shaped adapter
means the same adapter serves Twilio later with a base-URL change. Its webhook signature
scheme is Twilio's HMAC-SHA1-over-sorted-params, which is genuinely different from
Bandwidth's Basic auth and Telnyx's Ed25519 — so verification is per-adapter, as the
protocol always intended.

## Allowed files

Additive, except the four integration points:

- `app/providers/registry.py`, `app/providers/health.py` (new)
- `app/providers/telnyx/*`, `app/providers/signalwire/*` (new)
- `app/routing/*` (new)
- `app/models/routing.py` (new) + one additive migration
- `app/config.py` — SignalWire settings block only
- `app/providers/base.py` — `build_carrier` → registry shim
- `app/services/messaging.py` — send path consults the router
- `app/api/routes/webhooks.py` — per-carrier paths
- `app/main.py` — build the registry at boot
- tests

## Forbidden

- editing any P1/P2/P3 seam test to accommodate the new shape
- a settings flag that makes cross-carrier failover the default
- any code path that substitutes a carrier after an explicit operator override

## Test spec

Unit:
- [ ] precedence: explicit `from` > explicit `carrier` > sticky > policy > default
- [ ] an explicit carrier that is unhealthy **errors**, never silently substitutes
- [ ] breaker opens on N consecutive retryable failures, not on auth/invalid_request
- [ ] breaker half-opens after the cooldown and closes on one success
- [ ] Twilio-shaped signature verification accepts a known-good and rejects a tampered body

Integration:
- [ ] intra-carrier failover picks a second number on the same carrier
- [ ] cross-carrier failover is refused for a message in an existing thread
- [ ] cross-carrier failover succeeds for new outreach when the org opts in
- [ ] the carrier actually used is recorded on the message row
- [ ] each carrier's webhook path verifies with only its own scheme

Pass criteria: all green, ruff clean, and the P1/P2/P3 seam tests untouched.
