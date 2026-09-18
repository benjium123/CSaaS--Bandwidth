# Console authentication UI — "The Exchange"

Built 2026-09-18 over the P42/P43 auth system. Front-end only: no backend, migration, nginx
or CSP change. Coordinated with the P41–P43 audit session and the trust-and-safety session
throughout; their reviews are reflected below.

## Why

`App.tsx` wraps the signed-in Shell in `.dark`, but every unauthenticated route renders
**outside** that Shell, so the whole front door read `index.css`'s light `:root` tokens — a
white page with a 1px grey box — and signing in dropped you into a near-black console.
`SecureAccountPage` was the only unauthenticated screen that wrapped itself in `dark`. An
enterprise-grade auth system had a front door that did not look like part of the product.

## What it looks like, and why that

Telephone-exchange hardware rather than generic SaaS: a cold ink field with a patchbay
lattice, warm bone type, copper hairlines (the copper pair), and one verdigris — oxidised
copper — reserved for "live / verified". Amber and red are **not** spent on branding;
`primitives.tsx` already gives them to warning and danger, and a sign-in screen is the last
place to make a colour mean two things.

Type is Archivo Variable (its width axis sets headings at nameplate width) and Martian Mono
for codes, IPs and eyebrow labels — 111 KB of latin woff2, self-hosted via `@fontsource`
because the console's CSP (`font-src 'self' data:`) rules out any font CDN. Atmosphere is
pure CSS gradients and masks plus one inline SVG: `img-src 'self' data: blob:` means no
decorative asset can be fetched. One orchestrated page-load stagger, all of it behind
`prefers-reduced-motion`.

`src/auth/authTheme.css` defines **only** a scope class. It re-declares the same CSS
variables `index.css` does, and custom properties inherit, so the vendored primitives pick
the palette up without a forked component library. No global selector is touched, so the
console's own look is unchanged.

## Rules the code obeys

These came out of the P41–P43 audit and are written into the files, not just here.

1. **A failed sign-in reads the same whatever caused it.** Wrong password, 423 lockout, and
   an address with no account are answered identically by the server so sign-in cannot be
   used to discover which emails exist. No page branches on cause for wording; `AuthAlert`
   has no code-keyed variants, so there is nowhere for a per-cause sentence to live.
   `AuthSurfaces.test.tsx` pins this with two different codes and messages.
2. **Never state a security property while data is loading.** `me?.x &&` is safe — it
   renders on truth, and unknown degrades to nothing. `!me?.x` is dangerous — it renders on
   falsity, which is also the value of "not loaded". Anything that ASSERTS is guarded with
   `me == null ? null :` or `me &&`. The rule is in `AuthShell.tsx`'s header.
3. **Render server state; compute no security conclusions.** Messages are rendered verbatim
   (`MonitoringBanner`'s pattern). Absence of data produces silence, never an all-clear.
4. **No step ends without a way on, and no exit discards what cannot be recreated.** The
   test for an exit is "does this destroy something they can't recover?", not "is there a
   button?".
5. **A passkey ceremony is never retried in place.** The server burns a challenge on any
   outcome, so "try again" always requests fresh options.
6. **`code` drives affordance, never copy.** `sso_required` opens the SSO panel; it does not
   change a sentence. No pre-auth "does this email use SSO?" lookup exists or may be added —
   it would be an account-enumeration oracle.

## Restyled

`LoginPage`, `PasswordResetPages`, `RecoverAccountPage`, `SecureAccountPage`,
`AcceptInvitePage`, `SsoCallbackPage`, `OrgPickerPage`, `ReportNumberPage`,
`components/security/StepUpDialog` (presentation only — no decision moved),
`components/security/PasskeyGraceBanner`.

New: `src/auth/authTheme.css`, `src/components/auth/AuthShell.tsx`,
`src/pages/AuthSurfaces.test.tsx`.

`/report` deliberately wears none of the sign-in furniture: the person reading it was just
cold-called, has no account and owes us nothing, so it is a public safety form with no
identity column — which is also what lets it render correctly mounted inside the signed-in
shell.

## Behaviour changed, not only appearance

- **LoginPage**: a passkey-only account on a browser without WebAuthn used to get a button
  that failed inside `navigator.credentials.get` with no explanation. The control now stays
  in the tab order with `aria-disabled` (not `disabled`), `aria-describedby` points at a
  fixed sentence about the BROWSER, and the click handler returns early — `aria-disabled` is
  advisory and does not prevent activation. An empty `methods` list is handled too.
- **LoginPage**: `sso_required` (returned only after a correct password) opens the SSO
  hand-off instead of leaving a dead end. The error carries no org slug, so it cannot
  deep-link a workspace — enumeration-safe by construction.
- **AcceptInvitePage**: "At least 10 characters" removed. The minimum is a deployment
  setting and the policy also refuses breached passwords, so any number promises an
  acceptance the server will not honour. The shape is described; the refusal is verbatim.
- **OrgPickerPage**: "You are not a member of any organization yet" no longer appears while
  `/auth/me` is in flight; a statement about the request is shown instead.
- **RecoverAccountPage**: "Start this recovery again" clears the spent pending token —
  offered only AFTER the server has refused, because unprompted it would discard a live one.
- **EnterpriseSsoCards**: new identity-provider certificate surface — expired, inside 30
  days, and silence beyond that or when the backend sends no fields. Sign-in still works
  when expired and the copy says so. No renew action; there is no endpoint.
- **AuthContext**: `LoginResult`'s error arm carries `ApiError.code` (affordance only).

## Fixed after review — the permission helper

`hasPermission` in `src/auth/AuthContext.tsx` returned **true** for `!me || !orgId`. `me` is
null until `/auth/me` answers, so for the duration of every page load all eleven call sites
rendered affordances the person may not hold — including role editing and member reset on
TeamPage. The console already contained the correct pattern and the argument against this
one: `api/capabilities.ts`'s own fallback comment says *never show an admin item to an agent
because a lookup came back undefined*, and `navGating.verify.test.tsx` records the same
class of defect ("the capabilities fallback fails fully open") being fixed there earlier.

Not a privilege escalation — the backend enforces every one of these with
`require_permission`, so the affordance was a lie rather than a door — but the UI asserted a
security property it had not earned, in the permissive direction. Now denies what it does
not know; a control appears a beat late instead of appearing and vanishing.

The second fail-open branch is untouched and still deliberate: `membership.permissions`
being undefined means an older backend that predates the field, which is feature detection,
not ignorance. Two hand-rolled `Boolean(orgId) &&` workarounds (MonitoringBanner,
VerificationBanner — the two sites where the answer gated a fetch, which is presumably why
they were noticed) are removed, since leaving them would read as superstition.

`AuthContext.test.tsx`'s "fails open with no user or no org selected" was **pinning the
defect**, and its expectation is inverted with a note saying so.

One thing deliberately NOT changed: `hasPermission` also returns true when `orgId` names an
org the user has no membership for. That is a separate question from the loading one and is
raised rather than silently altered.

## Deliberately NOT restyled

`components/settings/` security cards — **SessionsCard, LoginHistoryCard,
OrgSecurityPolicyCard, AccountSecurityCards** — keep the console's existing look, which is
consistent with the pages around them. `components/kyc/` is untouched. Login risk flags
(`new_country`, `tor`, `impossible_travel`, …) still have no surface beyond the raw
login-events list. Do not infer from "the auth UI was rebuilt" that the whole security
surface was: unstyled-and-consistent is a fine state, unstyled-and-assumed-styled is not.

## Tests

`src/pages/AuthSurfaces.test.tsx` (new) plus the existing `P41TrustSafety` (17),
`P42Auth`, `LoginPage`, `AcceptInvitePage`, `p25SsoFlow`, `AuthContext`,
`EnterpriseSsoCards` (+4 new), `App`, `navGating`. Full suite: **936 passed, 1 failed** —
`AgentPage`'s end-to-end assistant test, which passes alone in 818 ms and took 5266 ms under
the full run. A first run failed 7 tests in a different, disjoint set; neither set touches
auth. The audit session then ran both named failures together on an idle machine — 2 files,
8 passed — so the evidence runs both ways: not regressions.

**RESOLVED — and the diagnosis recorded here first was wrong on both counts.** The original
note said both tests needed "await the typed value rather than asserting immediately after
`userEvent.type`". That was inferred from one symptom summary without reading either
component, and it was wrong twice. Both are now fixed with the real causes; the full suite
runs **86 files / 939 tests, all passing**.

*`p26VerifyComposer > clicking a quick-pick entry returns focus to the message field`* —
not a typing race. The field already held the complete `/gre`; that string is exactly what
was typed, so the "prefix" appearance was a coincidence. The quick-pick list is debounced
200ms (`Composer.tsx`), so the first listbox to appear is rendered from the PREVIOUS search
term; when the debounce fires, the query key changes, the new query has no cached data, and
React discards the option nodes. A handle taken before that point is detached, and clicking
it silently does nothing. A probe reproduced it deterministically with no artificial load:
`sameNode: false` across the debounce, and clicking the stale handle left the field at
`/gre`. **`waitFor` around the value assertion would not have fixed it** — the click never
ran, so the value never arrives; it would only have failed slower. The fix waits for the
debounced fetch (`/api/v1/templates?q=gre`) and re-queries the node before clicking.
Verified against the standard that separates a real fix from a wider window: with the delay
pushed to 1500ms — over four times the 350ms that reproduced the failure — the old shape
still fails and the new shape stays green, because it waits on a condition rather than a
duration.

*`AgentPage > creates, renames, sets a default and deletes an assistant end to end`* — not
a race at all. The real error was `Test timed out in 5000ms`. It runs in ~850ms idle and
lost the CPU to 85 sibling files. Re-running all of `src/pages` at a deliberately tight
1200ms budget produced six failures, **every one of them a timeout and not one assertion
failure**, which is what distinguishes a budget problem from a race. Fixed by raising
`testTimeout` to 20000 in `vite.config.ts`, with that evidence recorded beside it. A timeout
cannot hide a race, because a race surfaces as an assertion failure.
