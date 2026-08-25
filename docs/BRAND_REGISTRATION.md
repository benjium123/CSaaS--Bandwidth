# Track R — Brand & Campaign Registration (10DLC + Toll-Free)

> **This is a standing parallel track, not a phase.** It starts on day 1 and runs alongside
> P0–P4. It is sequenced this way because **registration is a wall-clock wait, not a work
> item** — days to weeks of third-party vetting that no amount of engineering speeds up.
>
> **Nothing sends production A2P traffic until Track R is green.** An unregistered number
> gets Bandwidth error **`4476` blocked-unregistered** — the message is rejected, not queued.

---

## Why this is the highest-priority non-code task

1. **It blocks revenue traffic, not just tests.** Every SMS phase (P1, P2, P3, P11) can be
   *built* against a test number, but none can be *used* for real A2P volume until a brand
   and campaign exist and the numbers are linked to that campaign.
2. **Throughput is now Trust-Score-driven** (change effective March 2026) — per-carrier MPS
   is a function of your Brand Trust Score plus campaign use case, **not** the old flat
   1 MPS. Bad or late registration doesn't just delay you, it permanently caps your
   send rate. Code written against a fixed 1 MPS assumption will mis-send.
3. **Toll-free verification is a completely separate track** with a different timeline
   (3–6 weeks) and different failure modes. 10DLC brand approval does **not** cover
   toll-free numbers. Teams routinely discover this a month too late.
4. Vetting rejections cost a full cycle. Getting the submission right the first time is
   worth more than starting a day earlier with sloppy data.

---

## The dependency chain (strictly ordered)

```
Bandwidth account with 10dlcCampaigns feature flag enabled
        │
        ├── API user granted the "Campaign Management" role
        │
        ▼
   BRAND registered  ──(optional)──►  Brand Vetting  ──► higher Trust Score
        │                                                      │
        ▼                                                      ▼
   CAMPAIGN registered (use case chosen)  ◄─── throughput tier is set HERE
        │
        ▼
   NUMBERS linked to the campaign
        │
        ▼
   A2P traffic permitted on those numbers
```

Toll-free runs entirely in parallel and shares none of these steps:

```
Toll-free number acquired ──► TFV submission ──► webhook: approved | denied(+reason)
```

---

## Prerequisites — confirm these before submitting anything

| # | Prerequisite | Where |
|---|---|---|
| 1 | Bandwidth account reaches production (risk **R1**) | Account/sales |
| 2 | `10dlcCampaigns` feature flag enabled on the account | Bandwidth support |
| 3 | API user holds the **Campaign Management** role | Dashboard → Users |
| 4 | Legal entity details final: legal name, EIN, entity type, address, vertical, website | Your records |
| 5 | Website is live and describes the business matching the brand | Public |
| 6 | Opt-in flow documented with evidence (screenshot/URL of the consent capture) | Required by vetting |
| 7 | Sample messages written, including the mandatory STOP/HELP footer language | See below |
| 8 | Privacy policy + terms URLs live, and the privacy policy mentions SMS | Public |

**The #1 cause of rejection is a mismatch** between the brand's stated business, the website
content, and the sample message content. Make those three tell one story.

---

## Bandwidth 10DLC API

Bandwidth is a **TCR (The Campaign Registry) CSP partner**. Registration is available both
in-dashboard and via API.

| Purpose | Endpoint |
|---|---|
| Create campaign | `POST /api/accounts/{accountId}/campaignManagement/10dlc/campaigns` |
| List campaigns | paginated `GET` on the same path |
| Brand vetting | Brand Vetting API |
| Reseller/brand | Reseller and Brand API |
| Bulk import existing TCR campaigns | Campaign Imports API |

**Campaign-management rate limits are separate from send limits:**
30 req/min GET (burst 20) · 10 req/min PUT/POST/DELETE.

**Toll-free verification** has its own dedicated API — no extra cost, submits to the
toll-free aggregator programmatically, and fires a **webhook on approval or denial with the
denial reasoning included**. Wire that webhook; don't poll a dashboard.

## Telnyx 10DLC (failover carrier — register here too, eventually)

| Purpose | Endpoint |
|---|---|
| Create brand | `POST /v2/10dlc/brand` |
| Create campaign | `POST /v2/10dlc/campaign` |
| Link numbers | `POST /v2/10dlc/phoneNumberCampaign` |

Known fees: **~$4 non-refundable per brand creation**; campaign charged **first 3 months
upfront** — roughly $15 charity / $6 low-volume mixed / $30 standard, by use case.

**Telnyx has mock brands and campaigns for sandbox testing** — use these to build and test
the registration code path without burning real fees or a real vetting cycle.

> Register on Bandwidth **first** (primary carrier, real traffic). Register on Telnyx before
> P14, so failover isn't blocked by an unregistered brand at the moment you need it.

---

## Use case selection — this sets your throughput ceiling

Choose deliberately; changing it later means re-vetting.

| Use case | Typical fit | Note |
|---|---|---|
| Low-Volume Mixed | early testing, mixed content, low MPS | cheapest, lowest throughput — fine to start, **plan to migrate** |
| Standard / Mixed | production multi-purpose sending | the usual production answer |
| Charity | 501(c)(3) only | |
| Sole Proprietor | single-person entity, no EIN | **very low throughput cap** — avoid if an EIN exists |

**Brand vetting** (an optional paid external vet) raises the Trust Score and therefore the
throughput tier. If volume matters, budget for it — it is usually cheaper than the revenue
lost to a throttled campaign.

---

## Mandatory message content

Every campaign submission must include sample messages, and the running program must honour:

- **STOP / STOPALL / UNSUBSCRIBE / CANCEL / END / QUIT** → opt out, send one confirmation
- **HELP / INFO** → reply with program name and contact
- **START / YES / UNSTOP** → opt back in
- First message of a program includes program name, message frequency, "Msg & data rates
  may apply", and how to stop.

> ⚠ **Whether Bandwidth auto-intercepts STOP/HELP server-side is UNVERIFIED.** Assume it does
> not. **We implement keyword handling ourselves** — this is built in **P3**, and per
> ARCHITECTURE D7 a STOP suppresses **the entire number pool**, never just the number that
> received it.

---

## Timeline expectations

| Step | Realistic elapsed |
|---|---|
| Brand registration | hours – 2 days |
| Brand vetting (if used) | 1–5 business days |
| Campaign registration + carrier approval | 1–7 business days |
| Number linking | minutes once approved |
| **Toll-free verification** | **3–6 weeks** |
| Port-in (if bringing numbers) | days – weeks; use the LNP checker first |

⚠ Bandwidth does not publish provisioning SLAs. Treat all of the above as estimates and
start early.

---

## Track R status

| Step | Status | Owner | Notes |
|---|---|---|---|
| R1 — Bandwidth account reaches production | 🔴 **blocker** | user | gates everything |
| `10dlcCampaigns` flag enabled | ⬜ | user | ask Bandwidth support |
| Campaign Management role on API user | ⬜ | user | |
| Legal entity + vetting data assembled | ⬜ | user | see prerequisites table |
| Opt-in flow documented with evidence | ⬜ | user | most common rejection cause |
| Brand submitted (Bandwidth) | ⬜ | | |
| Brand vetting ordered | ⬜ | | decide: worth it if volume matters |
| Campaign submitted (Bandwidth) | ⬜ | | use case: **decide before submitting** |
| Numbers linked to campaign | ⬜ | | |
| Toll-free numbers acquired | ⬜ | | only if we need toll-free |
| TFV submitted | ⬜ | | 3–6 week clock — start early |
| Telnyx brand + campaign | ⬜ | | before P14 |

Status: ⬜ not started · 🟡 submitted/waiting · ✅ approved · 🔴 blocked/denied

**Update this table whenever a submission moves.** `docs/PROGRESS.md` links here.

---

## What gets built in code (and when)

Track R is mostly *paperwork and waiting*. The **code** that automates it is **P4**:
- number search / order / release / configure
- port-in with LNP check
- brand + campaign registration via API
- TFV submission + the approve/deny webhook handler
- number ↔ campaign linkage in our own schema
- **throughput ceiling read from the campaign, not hardcoded** — this is the P4 detail that
  the March 2026 Trust-Score change makes load-bearing

**Do the first brand and campaign submission manually through the Bandwidth dashboard, now,
in parallel with P0.** Do not wait for P4 to automate it. The API automation exists so that
*future* brands/campaigns are cheap — it is not on the critical path for the first one.
