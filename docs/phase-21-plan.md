# Phase 21 — Smart routing (Fable, 2026-09-10)

## Goal
Every outbound text and call picks its route automatically and can say why. One customer
switch: **Smart routing: On** (default) or **Prefer a provider**. Failover across providers
becomes the default for texts when Smart routing is on, and voice finally walks the same
plan (closes D28). Nothing new to configure for a fresh workspace; an admin can still pin a
provider from Settings → Providers → Advanced.

## What exists (verified 2026-09-10)
- Per-provider circuit breaker (`providers/health.py`: `Breaker`, `opens_breaker`, `Health`).
- Org routing policy (`models/routing.py::RoutingPolicy`: `preference` list,
  `allow_intra_carrier_failover`, `allow_cross_carrier_failover`, `pinned_carrier`) with
  GET/PATCH at `/api/v1/routing/policy`; `/routing/carriers`, `/routing/catalog`, probe.
- SMS failover already walks an ordered candidate list (P3b/P14) in the send path
  (`services/sender.py`); voice does NOT (`routes/calls.py` dials one provider; `CreateCallResult`
  has no error taxonomy so the breaker cannot be fed).
- Number reputation stats + alerts (`services/reputation.py`), rate cards (`services/spend.py::
  resolve_rate`), per-org provider accounts (P17), per-message/call `carrier` columns.

## Design (final)
1. **Ranking function** `services/smart_routing.py::rank_routes(session, org_id, *, kind,
   from_number, to_e164) -> list[RouteCandidate]` where kind ∈ {sms, mms, voice}. A candidate
   = (provider, number_id | None, score, reasons[]). Filters first, then score:
   - capability: provider account active AND supports kind (catalog); number's provider
     must own the number for kind=sms/mms (a text must leave from the number it belongs
     to; failover for texts means "another number of ours on a healthy provider" only
     when the org has enabled cross-provider failover — today's rule, unchanged);
   - health: breaker not open (open = excluded). Half-open: if the breaker's single probe
     token is available the candidate ranks as HEALTHY (penalty 0) so the probe is spent
     and P14's automatic recovery still works; a half-open breaker whose token is already
     spent keeps the penalty. (Fable amendment 2026-09-10 after the Opus supervisor showed a
     fixed penalty starves recovery.)
   - reputation: number not in a breach state from `check_reputation` (breach = excluded
     for campaigns, penalised for 1:1 replies);
   - cost: `resolve_rate(provider, metric)` — lower cost scores higher, weight 1.0;
   - preference: `RoutingPolicy.preference` order adds a bonus; `pinned_carrier` short-
     circuits ranking (pinned first, others only as failover when allowed).
   Score = 100 − 40·health_penalty − 30·reputation_penalty − 20·normalised_cost +
   10·preference_bonus. Deterministic; ties broken by provider name.
2. **Explainability**: `messages.route_reason` and `calls.route_reason` VARCHAR(255)
   (migration 0039) hold one plain sentence: "Sent via Telnyx — cheapest healthy route",
   "Sent via Bandwidth — your preferred provider", "Failed over to Telnyx — Bandwidth
   unavailable". Shown on the message bubble tooltip and the call card.
3. **Voice failover (D28)**: `CreateCallResult` gains `error: CarrierError | None` using the
   same taxonomy the SMS path feeds into `opens_breaker`; `routes/calls.py` and
   `services/dialer.py` iterate `rank_routes(kind="voice")` exactly like the SMS sender:
   attempt → on a breaker-opening error mark and try the next → record `route_reason`.
   The walk honours `allow_cross_carrier_failover` and `pinned_carrier` exactly like the
   SMS walk (policy off = one attempt, plain failure sentence).
   Human calls over LiveKit SIP use the SIP trunk list (Telnyx today) — the ranking applies
   to provider-API calls (AI assistant, BXML paths); the LiveKit path records
   `route_reason="Via your calling trunk"`.
4. **Defaults**: new orgs get `allow_cross_carrier_failover=True` when Smart routing is on
   (seeded in `services/defaults.py`); existing orgs keep their current policy unless an
   admin flips the switch. The switch lives in Settings → Providers (top, one line) with
   "Prefer a provider" revealing a single dropdown; the breaker/health panel stays behind
   Advanced.
5. **No new nav.** Explainability rides on existing surfaces. Simplification: the
   `/routing/policy` form's three booleans collapse into the one switch + dropdown in the
   UI (the API keeps the fields).

## Schema (migration 0039, Fable-only)
- `messages.route_reason` VARCHAR(255) NULL; `calls.route_reason` VARCHAR(255) NULL.
- `routing_policies.smart_routing` BOOL NOT NULL DEFAULT true.

## Allowed files
Backend (corrected 2026-09-10 to the files that actually hold these paths): `services/
smart_routing.py` (new), `routing/router.py` + `services/messaging.py` + `routes/messages.py`
(the real SMS walk and where route_reason is written), `services/calls.py` (the real dial
path), `providers/voice.py` (`CreateCallResult.error`), `providers/{bandwidth,telnyx,twilio,
plivo,signalwire}/voice.py` (error taxonomy on rejection — same mapping the SMS adapters use),
`routes/routing.py` (smart_routing field), `services/defaults.py`, tests `tests/test_p21_*.py`.
`services/sender.py` is sticky-sender selection only and `services/dialer.py` dials LiveKit
only — neither is in scope.
Frontend: ProvidersPage (switch + dropdown), message bubble tooltip + call card reason
(components/conversations/Timeline.tsx), tests.
Forbidden: migrations (Fable writes 0039), models, `.env`, `deploy/**`, LiveKit/SIP code.

## Test spec
Unit:
- [ ] rank_excludes_open_breaker_and_penalises_half_open
- [ ] rank_excludes_breached_number_for_campaign_but_penalises_for_reply
- [ ] rank_prefers_cheaper_provider_all_else_equal (rate card drives order)
- [ ] rank_pinned_provider_first_and_failover_only_when_allowed
- [ ] rank_is_deterministic_and_tie_breaks_by_name
- [ ] sms_send_records_route_reason_sentence; sms_failover_records_failed_over_sentence
- [ ] voice_create_call_walks_plan_on_breaker_error (D28 regression: mutate the loop to a
      single attempt and the test must fail)
- [ ] voice_non_breaker_error_does_not_trip_breaker (an invalid_request is ours; auth errors
      ARE carrier faults per P14 DR-1 and do trip it — do not weaken health.py)
- [ ] half_open_probe_token_ranks_healthy_and_success_closes_breaker
- [ ] voice_walk_honours_cross_provider_policy_off_single_attempt
- [ ] livekit_human_call_records_trunk_reason_and_skips_ranking
- [ ] defaults_seed_cross_provider_failover_on_for_new_org; existing_policy_untouched
- [ ] policy_patch_smart_routing_false_requires_pinned_or_preference
Integration:
- [ ] Two providers, one breaker opened by a real provider-error fixture → the next send
      and the next AI call both go out on the other provider with the failover sentence.
Frontend:
- [ ] Switch + dropdown render from policy; message bubble shows the reason tooltip.
Manual: one text and one assistant call with a deliberately disabled provider.

## Deploy
yes (migration 0039 additive; `smart_routing` default true changes nothing for orgs with
one provider).
