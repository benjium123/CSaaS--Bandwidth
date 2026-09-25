# P44 fraud prevention: deploy checklist (operator)

Branch `p44-fraud` sits on top of `b1-billing-v2`. Deploy billing v2 first.

## Before deploy
1. **Mark the live workspace as established**, or it counts as a new account for 30 days
   from its creation. A new account gets a $25/day spend ceiling and no auto-recharge.
   The Ops limits form doesn't have these fields yet, so call the API as an admin:
   `POST /api/v1/ops/applications/{org_id}/limits` with
   `{"limits": {"established": true, "daily_spend_micros": <daily ceiling>, "max_concurrent_calls": <n>}}`.
   These three keys survive later edits made in the Ops limits form. The defaults are
   5 concurrent calls and $250/day for established accounts.
2. Decide E911 enforcement. Enforcement starts on `E911_ENFORCEMENT_START` (2026-09-26),
   and existing numbers get `E911_GRACE_DAYS` (7) more. A number bought later gets 7 days
   from its purchase. After that, calls from a number without an active 911 address are
   refused. **Until one E911 activation has been checked live on a Telnyx number and a
   SignalWire number, set `E911_ENFORCED=0`.** The carrier payloads follow the docs and
   have not been exercised against a live account yet.

## Deploy
- Migrations `0068_e911` and `0069_porting` (additive only).
- `python deploy/fraud_carrier_limits.py show`, then `apply`. This sets the Telnyx
  outbound voice profile to US-only, the maximum destination rate, the daily spend limit
  and the concurrency limit, plus the messaging-profile whitelist. It only touches objects
  whose names start with `csaas`.
- SignalWire has no API for this. In the dashboard, set geo permissions to the US only.
- Telnyx port-out PIN: Mission Control → Account Settings (see PORTING.md).
- Stripe Radar rules (Dashboard → Radar → Rules):
  - `Block if :cvc_check: = 'fail'`
  - `Block if :risk_score: > 75`
  - `Request 3D Secure if :risk_level: != 'normal'`
- Subscribe the Stripe webhook to `radar.early_fraud_warning.created`,
  `charge.dispute.created` and `charge.dispute.closed`.

## After deploy (live checks)
- Calls to +882…, +1-876… and +1-907… from the softphone and the API are refused.
- `fraud_carrier_limits.py show` reads back the US-only whitelist.
- Add a 911 address in Numbers → Emergency (911) addresses. It should go
  `pending` → `active` on one Telnyx number and one SignalWire number.
- A portability check on a real number (no submit).
- A Stripe test-mode early fraud warning refunds, holds the credit and pauses the
  workspace.

## Switches (env)
| Setting | Default | Off switch for |
|---|---|---|
| `DESTINATION_POLICY_ENFORCED` | true | the destination firewall |
| `FRAUD_EXPOSURE_ENFORCED` | true | concurrency, daily spend ceiling, auto-recharge caps |
| `E911_ENFORCED` | true | the 911-address call gate |
| `TOLLFREE_GUARD_ENFORCED` | true | toll-free inbound limits |
