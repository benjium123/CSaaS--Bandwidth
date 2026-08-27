# Phase 9b — Provider parity: bring your own key

## Goal
Four carriers at parity — **Bandwidth, Telnyx, Twilio, Plivo** (+ SignalWire, already
Twilio-shaped) — each usable by pasting credentials into `.env` and restarting. No code
change, no flag hunt, and the console tells you whether the key actually works.

## DR-1 — Keys ARE the switch (tri-state enable)
`*_ENABLED` becomes **tri-state**: unset/blank = **auto** (the carrier is live iff its
required credentials are present), `true`/`false` = explicit override. Rationale: the
current two-step (set the keys AND remember the flag) is a silent-failure generator — the
operator pastes a key, nothing happens, and nothing says why. Keys present is an
unambiguous statement of intent. The explicit `false` survives as a kill-switch, which is
the only case where the flag carries information the keys do not.

## DR-2 — Twilio reuses the SignalWire adapter shape, it does not subclass it
phase-3b DR-5 built SignalWire against the Twilio-compatible (LaML) API precisely so
"the same adapter serves Twilio by changing the base URL." Twilio gets its OWN package
with its own error table, signature verification and capabilities, sharing the
Twilio-compatible request/response *shape*. Not a subclass: SignalWire's quirks
(space URL, its own console codes) must never leak into Twilio, and a shared base class
would make every future Twilio-only fix a SignalWire regression risk.

## DR-3 — Plivo is the proof the seam is real
Plivo is the first carrier that is NOT Bandwidth-shaped, Telnyx-shaped or Twilio-shaped:
different auth (Basic auth-id/token), different signature (**V3: HMAC-SHA256 over
url + nonce**, header `X-Plivo-Signature-V3`), different XML dialect, different message
status vocabulary. If the CAL absorbs Plivo without touching `providers/domain.py`, the
abstraction is load-bearing rather than aspirational. If it does NOT, that is a finding
worth more than the adapter.

## DR-4 — Credential probes are explicit, never implicit
A "Test credentials" action performs ONE cheap authenticated read per carrier
(e.g. Twilio `GET /Accounts/{sid}.json`) and reports pass/fail with the carrier's own
message. It is operator-triggered only. The system never probes on boot: a probe on every
restart turns a credential typo into a rate-limit ban, and a startup that silently
"checks" credentials invites treating absence of an error as proof of health.

## DR-5 — Capability truth per carrier, declared not discovered
Each adapter declares what it can do (`CarrierCapabilities`, plus whether it implements
`NumberProvider` / `VoiceCarrier`). The console renders that matrix. An operator must be
able to see, before they buy a number, that (say) Bandwidth provisioning is unavailable in
this deployment — rather than discovering it from a failed order.

## Deliverables
1. `app/providers/twilio/` — messaging (LaML), voice (TwiML render + call control),
   numbers (search/order/release), webhook signature (HMAC-SHA1 over URL + sorted params).
2. `app/providers/plivo/` — messaging, voice (Plivo XML), numbers, V3 signature.
3. Config: tri-state enable for all five; new `twilio_*`, `plivo_*` blocks.
4. Registry: build every configured carrier; unchanged for existing ones.
5. `GET /api/v1/routing/carriers` gains capability + provisioning + voice flags;
   `POST /api/v1/routing/carriers/{name}/probe` performs DR-4's credential probe.
6. Console **Providers** page: per-carrier live/missing/capability matrix + Test button.
7. `.env.example` documents every carrier in one block with links to where each key lives.

## Test spec
- Twilio + Plivo: send success/failure classification, signature verify (valid, tampered,
  wrong key, replay), inbound parse (SMS + MMS), voice XML golden render, number
  search/order/release against mock transports.
- Tri-state: keys-only ⇒ live; explicit false + keys ⇒ dark; flag true + no keys ⇒ a
  status naming exactly what is missing.
- Probe: success and failure paths report the carrier's own message; probe is never
  called during registry build (assert no HTTP at startup).
- Routing fabric unchanged: existing precedence/failover tests stay green.

## Forbidden to implementers
`app/providers/domain.py`, `registry.py`, `health.py`, `voice.py`, `numbers.py` (the CAL is
frozen — if an adapter cannot be expressed through it, STOP and report), models, migrations.
