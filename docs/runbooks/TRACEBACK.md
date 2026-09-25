# Traceback and abuse-report runbook

A **traceback** is a request from the Industry Traceback Group (ITG, tracebacks.org) or
from an upstream carrier (Telnyx, SignalWire) asking which customer placed a call or sent
a text that someone reported as illegal (robocall, spoofing, fraud, SHAFT content).

**SLA: answer within 24 hours.** FCC rules require it of every voice provider in the chain.
Slow or missing answers get us listed as non-cooperative, and our upstream carriers can then
block all our traffic. Treat a traceback email like a production outage.

## 1. Acknowledge (within 1 hour)
Reply to the requester and say we are investigating. Record the request ID, the reported
calling number, the called number and the UTC time.

## 2. Find the traffic
Run this on the box (`ssh root@144.126.152.175`, then `docker exec -i csaas-db-1 psql -U csaas csaas`,
or the equivalent for the live compose project):

```sql
-- Calls: the reported caller ID around the reported time (+/- 1 hour, UTC)
SELECT c.id, c.org_id, o.name, c.direction, c.our_e164, c.contact_e164, c.status,
       c.created_at, c.duration_seconds, c.carrier
FROM calls c JOIN orgs o ON o.id = c.org_id
WHERE c.our_e164 = '+1XXXXXXXXXX'
  AND c.created_at BETWEEN '2026-01-01 12:00+00' AND '2026-01-01 14:00+00'
ORDER BY c.created_at;

-- Texts
SELECT m.id, m.org_id, o.name, m.from_e164, m.to_e164, m.status, m.created_at,
       left(m.body, 160) AS body
FROM messages m JOIN orgs o ON o.id = m.org_id
WHERE m.from_e164 = '+1XXXXXXXXXX'
  AND m.created_at BETWEEN '2026-01-01 12:00+00' AND '2026-01-01 14:00+00';

-- Who owns (or owned) the number
SELECT n.org_id, o.name, n.e164, n.status, n.purchased_at, n.released_at
FROM org_numbers n JOIN orgs o ON o.id = n.org_id WHERE n.e164 = '+1XXXXXXXXXX';
```

- **Nothing found:** the call did not originate with us. The caller ID was spoofed from
  outside, or the number is not ours. Reply saying so, with the query window you checked.
- **Found:** open the workspace in Ops → Console and in Ops → Applications (KYC). Note the
  verified business name, address, owner and the agreement version they accepted.

## 3. Answer the traceback
Give the ITG portal or carrier:
- our identity as the provider;
- that the traffic came from our customer (the business name from KYC);
- whether we are the originating provider.

The ITG portal asks for the "upstream" provider. For our own customers, **we** are the
originating provider. Never guess; if unsure, say what the records show.

## 4. Act on the customer
| Situation | Action |
|---|---|
| Clear illegal robocalls, spoofing, phishing | **Suspend** (Ops → Applications → Suspend, with `ban` on). This revokes sessions and keys, hangs up live calls and pauses campaigns (`services/suspension.py`). |
| Possibly legitimate (consented list, one complaint) | Pause (monitor), ask the customer for consent records within 48 h |
| Repeat within 90 days | Suspend + ban |

The suspend route requires a fresh second factor (step-up). Record the traceback ID in
the suspension reason.

## 5. Keep evidence
Export the rows above (CSV) and the customer's consent evidence into the case. Keep them
for at least 4 years. TCPA claims can be brought for 4 years.

## Related controls (P44 fraud prevention)
- Destination firewall: `services/destination_policy.py`
- Exposure caps and fraud signals: `services/exposure.py`, `monitor_calls._fraud_signals`
- Stolen cards / chargebacks: `services/card_risk.py`
- Carrier-side limits: `deploy/fraud_carrier_limits.py`
