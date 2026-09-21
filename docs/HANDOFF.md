# Handoff — state, decisions outstanding, and traps

<!-- ringlite-migration-note -->
> **Historical document.** Original brands, domains, carrier choices and
> deployment facts are retained verbatim below. The current product is
> **Ringlite**, target domain https://ringlite.io, primary carrier **Telnyx**,
> with deployment pending operator verification. See
> [RINGLITE_DOMAIN_CUTOVER.md](RINGLITE_DOMAIN_CUTOVER.md) for the domain cutover plan.


Written 2026-09-20 at the end of a long session. Everything below was verified at the
time of writing; re-verify anything you are about to act on, because this repo is worked
on by several sessions at once.

---

## 1. Where things actually stand

| | |
|---|---|
| Live site | https://csaas.sabinepropertygroup.net/ |
| Production box | `root@144.126.152.175`, app in `/opt/csaas` |
| Deploy | `bash deploy/deploy.sh` from a checkout — ships `git archive HEAD`, then runs `alembic upgrade head` ON THE BOX |
| Branch this session worked on | `integration` (identical to what was pushed to `main`) |
| Last commit I deployed | `56fb8d9` |
| Alembic head | `0056_merge_heads` |

**`main` is AHEAD of what is deployed.** Two commits from another session landed after my
last deploy and have NOT been shipped:

- `836d6bb` fix(livekit): room auto_create on — inbound SIP calls could never create their room (486 to the carrier)
- `3d9ce67` feat(p42): backend keeps LiveKit trunk numbers in sync; livekit-server v1.13.7

`836d6bb` reads like a real inbound-calling bug fix. **Deploying it is probably the first
thing to do**, but confirm with the owner first — see §4, because a deploy now also ships
everything else on `main`.

The production box has no git checkout (`deploy.sh` ships a tar), so you cannot ask it what
revision it runs. Track that yourself.

---

## 2. Logins and access

| Account | Password | Notes |
|---|---|---|
| `benethan011@gmail.com` | owner's own, not recoverable | Owner of **Sabine Property Group**. NO second factor enrolled. |
| `admin@apex-path.org` | `apex harbour lantern 47` | Owner of **Apex Path** (a test tenant I seeded). **Has a passkey enrolled.** |

Both are platform operators? No — only `admin@apex-path.org` was granted operator (`admin`
role) via `backend/scripts/make_operator.py`. The operator console at `/ops` refuses any
account without a second factor.

Database snapshots taken before each deploy, on the box:
`/root/csaas-pre-p41-*.dump`, `/root/csaas-pre-p46-*.dump`, `/root/csaas-pre-p47-*.dump`
(`pg_dump -Fc`). Take a new one before any migration.

A local dev backend + seeded SQLite DB lives in the session scratchpad
(`scratchpad/e2e/run_e2e_backend.py`, `e2e_flow.db`). It sets `LOOPBACK_CARRIER_ENABLED=true`,
which is how you exercise send/receive without carrier credentials. Vite proxies `/api` to
**port 8080**, so the backend must run there.

---

## 3. Production config — what is on and off, and why

In `/opt/csaas/.env` (deploy.sh never writes this file; edit by hand, back it up first):

```
KYC_ENFORCED=false              # deliberately off
TELEPHONY_PREPAID_DEFAULT=false # deliberately off
APP_ENV=production
```

`REQUIRE_2FA_PRIVILEGED_USERS` is **unset**, so the code default `true` applies.

**Why KYC_ENFORCED is false, and do not flip it casually.** The code default is `true`, and
with it on, `telephony_access.refusal()` returns `account_not_verified` for any org with no
`KycProfile` — which is every existing org. Turning it on without first creating and
approving a verification record **stops all texting and calling for every customer,
including the owner's own workspace**.

**Why TELEPHONY_PREPAID_DEFAULT is false.** Migration `0055` backfilled
`orgs.telephony_prepaid = true`, and I then set it back to `false` for both orgs by hand,
because their credit balance is 0 and prepaid enforcement would hard-stop their sending.
Current state: both orgs `telephony_prepaid = f`.

**The order to turn billing on**, when the owner is ready:
1. Add credits to the org.
2. Set `orgs.telephony_prepaid = true` for that org.
3. Only then consider `KYC_ENFORCED`, and only after an approved KycProfile exists.

Config guards that will refuse to boot (learned the hard way — I took production down once):
`REQUIRE_2FA_PRIVILEGED_USERS` must be true in production; `ALLOW_OPEN_REGISTRATION` must be
false in production; `LOOPBACK_CARRIER_ENABLED` must be false in production. Setting any of
these wrong crash-loops the API **after** `deploy.sh` has already shipped the files.

---

## 4. Decisions that need the owner, not an agent

1. **Deploy the two LiveKit commits?** See §1. Probably yes, but it also ships everything
   else currently on `main`.
2. **Forced password rotation.** An admin creating a user sets their password, so the admin
   knows it. Not implemented: the only migration-free signal (`password_changed_at IS NULL`)
   is true for *every* account that never changed its password, including the owner's, so
   keying rotation on it would prompt everyone. Proper fix is a `must_change_password`
   column, backfillable from the `account.created_by_admin` audit rows. Needs a migration,
   which needs the owner's approval now that we are live.
3. **Voice is priced below cost.** $0.005/min charged; the repo's own carrier cost table has
   outbound at $0.007–$0.014/min. SMS ($0.01 vs $0.004) and numbers ($15 vs $0.35–$1.15) are
   strongly profitable. The owner was told and chose to keep $0.005.
4. **Bring-your-own numbers.** `POST /api/v1/numbers` (a hand-entry seed endpoint) creates a
   number WITHOUT charging, but `renew_number_rentals` then bills it $15/month at the next
   cycle. That contradiction is untouched on purpose — it is a pricing decision.
5. **Non-owner credit visibility.** Billing is owner-only, so staff see no balance. Suggested
   follow-up: add a coarse `credit_warning` field to the every-member capabilities endpoint
   so an agent whose texts are failing learns why. Not built.
6. **A2P/10DLC vocabulary on the landing page** — see §7.

---

## 5. What is NOT built, and blocks going live properly

- **No carrier credentials are configured.** `.env` has none, so the carrier registry is
  empty and real sends fail with 503. Everything proven so far used the loopback carrier.
  This is the single biggest gap between "works" and "works for customers".
- **No registrar / TCR integration.** `services/registration.py` has NO outbound HTTP at all.
  `submit_brand`/`submit_campaign`/`submit_tollfree` validate, flip a status column, and
  return. Status only moves when someone POSTs the `/status` callback. So "approved" in this
  database means *a human marked it approved*, not that TCR approved anything. The UI copy is
  deliberately honest about this ("Mark brand ready to file", "Nothing is transmitted to a
  carrier from here") and there are tests asserting the misleading phrasings stay absent —
  **do not let that copy drift** when the real integration lands.
- **Migrations cannot run from scratch on SQLite.** `alembic upgrade head` dies at
  `0003_inbox_contacts` (add_column with a constraint needs batch mode). Production is
  Postgres and is fine. `tests/conftest.py` builds schema with `Base.metadata.create_all`, so
  **no test exercises migrations at all**.
- **Nobody has pointed a phone at the TOTP QR code**, and nobody has opened the Add-teammate
  drawer in a browser. Both are unverified by observation; `css: false` means no test can
  cover either.
- **Escape on mobile** closes both the number-access drawer and the rail sheet, dropping you
  to the conversation list. Real, untested, left alone rather than editing shared primitives.

---

## 6. Traps in this codebase — read before trusting a green test run

These all cost real time this session. They are not hypothetical.

1. **`vitest` runs with `css: false`.** No frontend test can prove a colour, a radius, or a
   layout. A passing suite says nothing about appearance. Verify styling in a browser or by
   grepping emitted CSS after `vite build`.
2. **A missing style-constant import stays GREEN.** Import a name that does not exist,
   `cn(undefined, …)` drops the class, the page renders unstyled, every behavioural assertion
   passes. `tsc --noEmit` catches it instantly. **For shared class constants, the typecheck is
   the real check and the suite contributes nothing.** Reproduced and confirmed.
3. **The test stub matcher answers sub-resources.** `makeStubClient` takes the LONGEST
   matching prefix, so a stub for `/api/v1/orgs/current` still answers
   `/api/v1/orgs/current/members` when no `/members` key is declared — handing an array-shaped
   query an object. The page throws inside an ErrorBoundary, the boundary eats it, the suite
   stays green while rendering a crash, and the unhandled error destabilises *other* tests in
   a full run. Stub each path you request. (I tried making the matcher strict; it broke 24
   tests across 9 files because prefix stubs covering sub-paths are a deliberate convention.)
4. **`.deepseek/` staging dirs were being collected by vitest**, running tests against
   proposed files that were never applied. Excluded in `vite.config.ts`. If you see the test
   count jump, check that exclusion still exists.
5. **`app.routes` is lazily populated.** Route tests written over it are vacuous — the list
   holds wrapper objects with `.path is None`, so a `for` loop never enters its body and
   passes having asserted nothing. Use `create_app(settings).openapi()["paths"]`.
6. **Two migrations were both numbered `0044`** on different branches with the same parent.
   Git merged them cleanly (different files, no conflicting lines) and alembic then had two
   heads, which aborts `upgrade head` — *after* deploy.sh has shipped the code.
   `0056_merge_heads` heals it. **Never renumber either `0044`**; `0044_messaging_health` is
   applied in production and changing its id strands that database. Check `alembic heads`
   returns exactly one before any deploy.
7. **A mutation that does not mutate proves nothing.** Twice this session a "mutation check"
   silently failed to apply and the re-run was testing the unmodified file. Always assert the
   mutation landed (grep for your marker) before believing a red or a green.
8. **Assert the specific outcome, not the status code.** The best example: committing a user
   before validating inboxes still returned `404` — the reassuring answer stayed reassuring —
   and what caught it was `assert get_by_email(...) is None`. A status-only test would have
   passed while leaving an orphan account.
9. **Do not run the full backend suite casually** — it takes about an hour. Run the files you
   touched plus their neighbours.

---

## 7. Landing page — research brief

The owner is writing the landing page themselves. Our landing page files were deliberately
EXCLUDED from the merge (`main` never had one; `/` falls through to the sign-in form). Four
agents researched competitors; the findings that matter:

**The uncontested gap.** Across ~15 marketing pages and 8 competitors (Quo, Dialpad, Aircall,
JustCall, CloudTalk, RingCentral, Grasshopper, Google Voice), **zero mention 10DLC/A2P as a
feature**. Grasshopper charges for it ($19.50 one-time + $1.50/month, buried in add-ons);
RingCentral hides it in disclaimers ("Additional TCR fees may apply"); CloudTalk footnotes it
in a country table. Buyers discover a 5–30 day vetting delay *after* paying. Say it as the
fear, not the acronym: **"your texts actually get delivered"**.

**OpenPhone is now Quo** — rebranded Sept 2025 with a $105M round; `openphone.com` 301s to
`quo.com`. Our console's inbox was modelled on the old OpenPhone.

**The real competitive threat is Grasshopper**, not RingCentral or Google Voice. It already
owns the sentence ("The best business phone app for entrepreneurs"), converts with **"Pick a
custom number"** rather than "start free trial", and at Small Business is $55/mo for 4 numbers
with unlimited users ≈ **$13.75/number, flat with unlimited minutes** — which prices under us.
Its FAQ pre-empts metering: *"Do I have to pay by the minute?" — "All of our plans include
unlimited minutes!"*. **If $15 + usage credits reads as a meter, we lose to $14 flat.** Credits
must be framed as an included allowance with a plain over-limit sentence.

**Where we win on price:** everyone advertises a price they do not charge monthly —
Quo $15 advertised / $19 monthly; Dialpad $15 / $27; RingCentral $20 / $30. Ours is $15
month-to-month. Aircall has a 3-licence floor ($90/mo); Dialpad's cheapest plan cannot add a
second number at all. We have no seat minimum.

**Craft (from Linear, Front, Attio, Twilio):** Linear ships no video, no canvas, no sticky, no
GSAP/Framer/Lottie and 6 script files. The gap is palette and shadow, not motion. Highest
value, cheapest first: the lit-card inset box-shadow; asymmetric hover (in `0s`, out `150ms`);
accent on under 1% of elements (Linear: 5 of 5,277); ONE animation — a conversation row
arriving; a sticky chapter rail that pins the navigation not the visual; a fixed-aspect DOM
mock with specific fake data. Do NOT copy Front's Lottie hero (27 base64 WebP slices, 5.4 MB
of script, needs a motion designer). Wrap motion in
`@media (prefers-reduced-motion: no-preference)` so the accessible path is the default.

**How to show a list-shaped product:** stage a *moment* inside the inbox (inbound text,
@mention to a colleague inside the thread, missed-call event, "New lead" chip, same thread
mirrored on a phone) rather than photographing an empty list. For feature cards, zoom 3× into
the one component proving the claim, delete the chrome, float it on transparency.

**One idea worth stealing outright:** Quo's AI page has a plain `tel:` link — you call their
AI and talk to it. No signup, no card. Put it on the homepage, which is where they failed to.

---

## 8. Working agreements the owner set

- Delegate implementation to `deepseek-coder` agents; review their work, point out errors,
  make them fix it. **They have no web access** — Bash/Read/Grep/Glob/Edit/Write only — so
  research needs a different agent type.
- Agents must NOT run git commands. The supervising session commits.
- Never invent Stripe price IDs. "It's real money you know."
- Agents must not run the full backend suite.
- Tell agents to push back on a false premise rather than work around it. Several of my briefs
  this session contained one, and every time the agent was right.
