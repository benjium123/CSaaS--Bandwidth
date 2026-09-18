# P41-P43 adversarial audit — joint closing statement

Branch `p41-kyc`, 2026-09-18. **Pushed** — `origin/p41-kyc` is at `8b6a1d8`, local 0 ahead,
verified by re-fetching rather than from the push output. 34 commits ahead of `main`, which is
untouched and unmerged. Written before the push; where an earlier sentence says "not pushed",
read it as describing the state at signature.

One session built P41-P43 (business verification, enterprise auth, AI safety monitoring). A
second session audited it adversarially at the operator's request. This is both sessions'
agreed account, written for whoever decides on the merge and the deploy. A third session is
concurrently rewriting the auth console UI in `frontend/` and has committed nothing; every
number below is backend and unaffected by it.

## (a) What was found and fixed — 12 findings, all fixed

1. **The events websocket never re-checked the platform idle timeout.** An unattended console
   kept streaming live customer events for up to the absolute session lifetime (~11.5h with
   defaults) after "signed out after inactivity". The platform floor now applies on every
   refresh and an idled-out session is revoked — but only when the *platform* floor ran out,
   because a workspace's tighter policy must refuse that workspace without ending the
   account-wide session.
2. **Cross-tenant leak in the workspace login-event list.** A shared member's sign-ins to
   OTHER workspaces — IP, device, outcome, risk flags, and in bulk via CSV — were returned to
   anyone holding ordinary admin permission. `LoginEvent` carries an `org_id` without being
   tenant-scoped, so that `WHERE` is the only boundary; the membership clause is now
   restricted to org-less (pre-workspace) sign-ins.
3. **`MONITOR_ENFORCED=false` silently released every already-paused account** rather than
   only stopping new pauses, while the ops queue and the customer's own banner still said
   "paused". It also contradicted the code's own stated invariant that only an operator ends
   a pause — a config flag was ending them. An existing pause is now honoured whatever the
   flag says.
4. **Local-vs-UTC date comparison.** A test failed for part of every day, and the production
   "incorporation date in the future" guard refused valid dates depending on server timezone.
   Both now UTC.
5. **Invite privilege escalation.** `create_invite` never applied `update_member`'s
   role-containment rule, so a member holding only `members:invite` could invite a second
   address of their own **as admin** and operate from it.
6. **The lockout was an account-existence oracle.** 423 for a real address, 401 for an unknown
   one — which also let an attacker lock out an entire staff list. Unknown addresses now lock
   identically, derived from their own event history, writing nothing.
7. **The first fix for 6 leaked the same fact through the escalation timer** on round two (a
   real account's second lock doubles; the synthetic one reset). It now replays the real state
   machine.
8. **Bare national numbers were always parsed as US.** A UK workspace's ordinary formats were
   refused outright, and a trunk-stripped one became a real number in Maine.
9. **The same default defeated consent.** An opt-out stored as `+1...` left the real number
   unsuppressed while the console showed "opted out". Consent paths now resolve the
   workspace's country and refuse a bare number when it is unknown rather than guessing.
10. **WebAuthn challenge consumption was read-modify-write**, so two simultaneous requests
    could both succeed — defeating the sign-count clone detection, the one mechanism that
    notices a duplicated authenticator. Now a conditional `UPDATE` with a rowcount check.
11. **The step-up dialog could render with no control at all** for a passkey-only person on a
    browser without WebAuthn: unable to confirm, unable to leave.
12. **The fix for 11 claimed "you have no way to confirm" while the user record was loading**,
    and offered a button that discarded the pending step-up.

Also hardened: SAML certificates refused at save when expired and surfaced as an alert at
sign-in rather than refused mid-flow; the SAML response bound to the browser that started it;
the Stripe Identity webhook secret no longer able to sign billing events; SSO/SCIM grants of
privileged roles alerted rather than silent; a non-USD `STRIPE_PRICE_CURRENCY` refused at boot
because every amount is USD with no conversion; an nginx rate limit for the unauthenticated
report-a-number route.

Commits: `579657a` (1-9 + hardening), `296b8cd` (11-12 + nginx), `00d1691` (10 + the blocked
half of 9), `270f7d1` (a third session's abandoned SignalWire work, committed unchanged and
unreviewed so it would survive), `ce54a1c` and `9adf466` (test defects found by running on
PostgreSQL), `2c8b757` (runbook).

## (b) What is verified, and how

- **The full suite has NOT run to completion on PostgreSQL.** The first attempt deadlocked
  (one test, fixed in `ce54a1c`); the second was stopped at 216 of 2058 tests once its
  projected runtime measured ~3.3 hours and its remaining value was auditing test quality
  rather than certifying the merge. In those 216 tests it found exactly two problems, **both
  test defects, zero product-behaviour differences.**
- Full backend suite on SQLite at `296b8cd`: **2045 passed, 9 skipped, 0 failed**, 50 minutes.
  The first complete run either session achieved.
- PostgreSQL, targeted, at `00d1691`: **129 passed** — both sessions' audit pins, sessions,
  SAML, SCIM, step-up, passkey policy, phone region, monitor fixes. This confirms the
  conditional-UPDATE code paths execute correctly on a database that does not serialise
  writes, which SQLite cannot show. It does **not** observe the races themselves: no test
  issues two simultaneous requests against a challenge or a step-up row, so that those fixes
  *prevent* the race remains reasoning about PostgreSQL row-locking, not an observed result.
  The only genuinely concurrent tests anywhere are two `pg_only` ones, and they cover webhook
  ingest and call creation rather than auth.
- The **6 `pg_only` tests**, which had never executed on any machine: 6 passed on PostgreSQL.
  Three bear directly on this audit — tenant isolation, concurrent duplicate webhook ingest,
  concurrent call creation.
- 11 further audit pins on PostgreSQL, run independently by the auditing session.
- `test_bugfix_area2` + `test_bugfix_area4` on PostgreSQL after the fixes: **45 passed**, on
  the instance that previously deadlocked on one and failed on the other.
- **Live end-to-end** at `00d1691` against the real AI: **128/128** — four verification
  applicants across US and UK, document reading, decision packs, texts, calls, the risk
  ladder, operator actions, and an opt-out recorded and then enforced.
- **18 regression pins across six files** from the auditing session; 9 tests from the
  implementing session.
- **Migrations: none needed.** No model or migration file changed in any commit, verified with
  `git status` as well as `git diff`, because a new migration is untracked and a diff cannot
  see it. Head remains `0051_monitoring.py`; the deploy is code-only.
- **Pushed on 2026-09-18**, `b8deb65..8b6a1d8`, clean fast-forward, `main` untouched. This
  changes the status of one commit and it is worth stating plainly: `270f7d1` — a third
  session's abandoned SignalWire work, committed unchanged and **reviewed by nobody** — has
  gone from local-and-unreviewed to **published-and-unreviewed**. Its test file has been
  executed exactly once (passing, by the auditing session, inside an unrelated 108-test run);
  nothing else about it has been read by either signatory. Publishing did not review it.

## (c) What remains unverified by anyone

1. **The frontend beyond a handful of auth components.** Six were reviewed by the implementing
   session; the auditing session reviewed none until reading a diff. ~900 vitest tests exist
   over code neither read. A third session has now rewritten the unauthenticated console UI.
   That work is committed as `22a1d84` (frontend and docs only - verified: it touches no
   backend file) and unmerged, and its status is: the
   auditing session reviewed it across four rounds and found one real defect (a pinned test
   that had become unable to fail, because the control it checked now rendered permanently
   disabled in jsdom — fixed, and pinned from both sides); the implementing session reviewed
   only the files it owned (StepUpDialog, verified as presentation-only against `git show
   HEAD:`) and supplied the contracts, the risk-signal shapes and the certificate spec. The
   auditing session's review covers `frontend/src/auth`, `components/auth`,
   `components/security` and one settings card; it has explicitly NOT read the rest of the
   console, and independently re-ran the auth suites on a clear machine (67 passed across 8
   files) and reproduced the two suspected flakes passing in isolation, so that conclusion is
   two-directional rather than one session's.
   CORRECTION, 18 Sept 2026, after this statement was first signed. An earlier version of this
   paragraph recorded two tests as an open follow-up with a diagnosis: `AgentPage > creates,
   renames...` and `p26VerifyComposer > clicking a quick-pick entry...`, attributed to
   asserting immediately after `userEvent.type` instead of awaiting the typed value. **That
   diagnosis was wrong on both tests, and it is now fixed in cdc9913.** The real causes:
   p26VerifyComposer was not a typing race at all — the list is debounced 200 ms, so the first
   listbox rendered belongs to the previous search term, and when the debounce fires the query
   key changes, the new query has no cached data and React discards the option nodes; a handle
   taken before that is detached, so the click silently does nothing (`sameNode: false` across
   the debounce, reproduced with zero artificial load). The recorded fix would have made it
   WORSE in the predicted way — wrapping the value assertion in `waitFor` waits for a value
   that never arrives because the click never ran, so it would have failed more slowly and
   looked like a deeper defect. AgentPage was not a race in any form: the error was `Test timed
   out in 5000ms`, invisible because that run had been piped into `tail`; running all of
   `src/pages` at a deliberately tight 1200 ms budget produced six failures, every one a
   timeout and not one an assertion failure, which is the discriminator between a budget and a
   race. `vite.config.ts` now sets `testTimeout: 20000` with that evidence beside it. The fixed
   shape was then held green at 1500 ms — over four times the 350 ms that reproduced the
   failure — which is the test that distinguishes a condition-based fix from a merely wider
   window. How the error got in, since that is the point of section (d): the auditing session
   asserted the typing-race mechanism from a symptom summary without reading either component;
   the third session recorded it in its own handover note; and the IMPLEMENTING session wrote
   it into this document in 95d2ce8, having checked neither the components nor the claim -
   accepting it because it came from the session that had been right about everything else
   that night. That is the specific failure: a claim's source was treated as evidence for it.
   Both signatories propagated a mechanism neither had checked. Its
   own handover note is docs/CONSOLE_AUTH_UI.md, which carries an explicit NOT-restyled list -
   SessionsCard, LoginHistoryCard, OrgSecurityPolicyCard, AccountSecurityCards,
   components/kyc/, and the login risk flags, which still have no surface at all. "The auth UI
   was rebuilt" must not be read as "the whole security surface was".
2. **WebAuthn against a real authenticator.** The server-side code was read end to end and
   delegates correctly to the library with every expected parameter. Nobody has run these
   flows against real hardware, a real browser or a security key; jsdom has no WebAuthn, so
   the browser half is unexercised by construction.
3. **Concurrency in the auth paths — NARROWED 18 Sept, not closed.** The WebAuthn challenge
   race IS now observed. Both signatories had written that racing it required two browsers;
   that was wrong, and the reason is worth keeping: `consume_challenge` runs BEFORE
   verification, so the claim can be raced with a credential that is merely well-formed — no
   crypto and no browser needed. Raced four times: exactly one winner each time, never two —
   but **observed on SQLite, which serialises writers**, so exactly-one-winner there is
   necessary evidence and not sufficient. On PostgreSQL, where writers genuinely contend, the
   conditional UPDATE's row locking remains reasoning. docs/WEBAUTHN_E2E.md carries the same
   caveat at its own §80.
   Still unobserved: simultaneous requests against a step-up row, and against the
   idle-session revoke. Those two remain code inspection plus sequential execution on
   PostgreSQL.
4. **Secret handling beyond the code.** argon2id parameters (t=3, m=64MiB, p=4), pinned JWT
   algorithms with nothing disabled, Fernet error handling and the SHA-256-over-256-bit-random
   API key choice were all verified by reading. Nobody checked where `CREDENTIALS_MASTER_KEY`
   comes from in a real deployment, whether key rotation has a procedure, or whether any
   secret reaches the logs.
5. **Deployment configuration beyond the auth half of nginx.** Untouched: docker-compose, the
   LiveKit compose, the SignalWire firewall script, TLS termination, and the real server's
   environment — including whether `REDIS_URL` is set there, which item 6 depends on.
6. **Multi-worker behaviour with Redis.** Everything ran single-process with `REDIS_URL` empty.
   Five per-process fallbacks exist — session revocation cache, SSO state, the SAML replay
   set, the rate limiter, and the phone-region cache. Four are safe or double-covered; the
   rate limiter is not, because its in-process fallback multiplies the effective limit by the
   worker count and that number IS the control. Nobody has run this system with more than one
   worker. This is the largest untested configuration and it is a deployment decision, not a
   code one.
7. **Quiet hours end to end.** 22 unit tests including the UK zone fix, but it was disabled on
   the test workspaces during the live run because the run happened at 03:44 in Texas and the
   gate was correctly holding US texts. Unit-covered, not flow-covered, for a legitimate
   reason.
8. **`agents/call_monitor.py`.** Never executed by anyone, no fake exists, needs two API keys
   that are unset. What is verified: it imports against livekit-agents 1.7, and
   `worker_config.sip_call_active` has 7 unit tests. Unverified by construction: that the
   backend's dispatch reaches a worker with this agent name; that the announcement publishes,
   is heard and unpublishes; that Deepgram streaming attaches to the right track and produces
   segments; that the flusher posts them; and that a room with no human, or a participant
   leaving mid-call, doesn't leave the worker running. The first real call is the test, and it
   should be made deliberately by someone watching `/ops` > Monitoring.
9. **The full PostgreSQL sweep**, as stated in (b).
10. **The orphaned SignalWire work in `270f7d1`.** Reviewed by nobody. Its test file has now
    been executed exactly once — by the auditing session, passing, inside a 108-test run —
    which makes it "run once, passed, unreviewed" rather than "never run". The two unregioned
    phone-parsing sites in that area are fixed; the rest of that work has had no review.
11. **Anything needing real external services:** no live carrier call, no LiveKit room, no
    Deepgram or ElevenLabs, no real Stripe keys for billing or Identity, and email is log-only
    without SMTP, so every "we emailed the owner" path is verified by call rather than by
    delivery.
12. **Load, performance and soak.** Nothing at all. A green functional suite says nothing about
    behaviour under traffic.
13. **Closed, stated positively so it is not re-litigated: migrations.** No model or migration
    file changed in any commit, verified with `git status` as well as `git diff`. Head remains
    `0051_monitoring.py`; the deploy is code-only.

## (d) How much of "verified" was cross-checked

Almost everything either session caught was caught by the **other** one checking. That is the
process finding, and it is separate from the twelve about the code.

The auditing session proposed a pass criterion for the PostgreSQL run ("skipped must drop to
zero") that was wrong, derived by counting grep hits rather than collecting tests; the
implementing session accepted it without checking and independently reproduced the same wrong
number. Both corrected within minutes. Applied naively it would have failed a correct run.

**Implementing session's process failures:** four test runs orphaned by stopping wrappers
instead of child processes, twice more with an embedded PostgreSQL left holding its data
directory; a dev server left running after the live end-to-end; a verification command pointed
at a directory that does not exist, which could only ever return "clean"; a reporting pipeline
that hid an hour of state twice; a suite started on top of the other session's without checking
the process list; and a runtime projection 4x wrong from misreading elapsed time.

**Auditing session's process failures:** ran a broad suite after committing in writing to
targeted runs only, without checking the process list, colliding with the very run this
statement's numbers depended on; left an orphaned PostgreSQL server by killing a wrapper rather
than its child — the same failure mode it had diagnosed in the other session an hour earlier;
and asserted that a test fixture was non-hermetic — that three concurrent suites had been
sharing a sanctions file and their results were therefore "unusable in both directions" —
without verifying it. The fixture already wrote to a per-test `tmp_path` and had been isolated
all along. That claim reached the other session's user before being retracted.

**Six distinct instances of one pattern were found: a mechanism that looks like verification
and cannot produce the answer it claims to test.** An exception handler that swallowed a
`NameError` into a log line nobody reads; a formula that agreed with the real state machine
until the state diverged; a git pathspec pointing at a directory that does not exist; a stop
command that killed wrappers and left four runs alive; optional chaining that could not
distinguish "false" from "not loaded yet"; and the skip-count criterion above.
