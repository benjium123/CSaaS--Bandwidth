import * as React from "react";
import { useNavigate } from "react-router-dom";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { missingLabel, useKycProfile, type KycPerson, type KycProfile } from "@/api/kyc";
import {
  ONBOARDING_STEPS,
  firstIncomplete,
  outstandingFor,
  type StepId,
} from "@/components/onboarding/onboardingSteps";
import {
  AuthAlert,
  AuthButton,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Lamp,
} from "@/components/auth/AuthShell";
import { cn } from "@/lib/utils";
import "@/components/onboarding/onboarding.css";

/**
 * The customer-facing verification journey: one screen per profile status.
 *
 * This page ORCHESTRATES, it does not edit. Every field lives on /settings/verification,
 * which owns the forms and the mutations; this decides what to do next and sends you there.
 * Two implementations of "is this section filled in" is a rule written twice and true in
 * one place.
 *
 * THREE RULES IT EXISTS TO KEEP:
 *
 * 1. NOTHING IS CLAIMED FROM AN UNLOADED RESOURCE. The profile owns every step's state, so
 *    while it is in flight this renders a skeleton - no ticks, no crosses, no status. A
 *    stepper asserts where someone is in a process, and asserting that from a value that has
 *    not arrived is the failure this codebase keeps finding.
 * 2. THE STEPPER IS ONLY MOUNTED IN `draft` AND `needs_info` - and the reason is a coupling
 *    worth stating, because `[]` is not self-explanatory. The serializer SHORT-CIRCUITS the
 *    field (`kyc.py:214`: `missing_for_submission(...) if profile.status in ("draft",
 *    "needs_info") else []`), so outside those two statuses `[]` means NOT COMPUTED, not
 *    "nothing left to do". A stepper rendered elsewhere would tick every step green off an
 *    absence - and beside a rejection, that is a completed checklist celebrating a refusal.
 *    If `missing` ever becomes populated in `submitted`, this guard must be revisited
 *    deliberately rather than a stepper appearing there by surprise.
 * 3. A BUTTON IS ONLY SHOWN WHERE THE SERVER WOULD ACCEPT THE CALL. See `Reverification`:
 *    only the person themselves may repeat their own ID check, so offering everyone a
 *    button offers most people a dead end.
 */
export function OnboardingPage() {
  const { api, me, orgId } = useAuth();
  const navigate = useNavigate();
  const profileQuery = useKycProfile(api, hasPermission(me, orgId, "org:read"));
  const profile = profileQuery.data;

  const toVerification = React.useCallback(
    (hash?: string) => navigate(hash ? "/settings/verification#" + hash : "/settings/verification"),
    [navigate],
  );

  if (profileQuery.isPending) {
    return (
      <AuthSurface>
        <div className="ob-skeleton" aria-busy="true" aria-label="Loading your application">
          <span className="ob-skeleton-bar" style={{ width: "40%" }} />
          <span className="ob-skeleton-bar" style={{ width: "72%" }} />
          <span className="ob-skeleton-bar" style={{ width: "58%" }} />
        </div>
      </AuthSurface>
    );
  }

  if (profileQuery.isError || !profile) {
    return (
      <AuthSurface>
        <AuthPlate eyebrow="Business verification" title="We couldn't load your application">
          <AuthAlert>
            {profileQuery.error instanceof Error
              ? profileQuery.error.message
              : "Something went wrong. Try again in a moment."}
          </AuthAlert>
        </AuthPlate>
      </AuthSurface>
    );
  }

  switch (profile.status) {
    case "draft":
    case "needs_info":
      return <Wizard profile={profile} onOpen={toVerification} />;
    case "approved":
      return <Approved profile={profile} onContinue={() => navigate("/inbox")} />;
    case "rejected":
      return <Rejected profile={profile} />;
    case "suspended":
      return <Suspended />;
    case "reverification_due":
      return (
        <Reverification
          profile={profile}
          onOpen={toVerification}
          onContinue={() => navigate("/inbox")}
        />
      );
    // `submitted` and `in_review` are one screen on purpose: the difference between them is
    // whether a reviewer has opened it yet, which is our internal business and not a fact
    // the customer can act on.
    default:
      return <Waiting profile={profile} />;
  }
}

/* ────────────────────────────────────────────────────────── the wizard ── */

function Wizard({
  profile,
  onOpen,
}: {
  profile: KycProfile;
  onOpen: (hash?: string) => void;
}) {
  const missing = profile.missing;
  const [open, setOpen] = React.useState<StepId | null>(() => firstIncomplete(missing));
  const ready = missing.length === 0;

  return (
    <AuthSurface>
      <div className="ob-wrap">
        {/* needs_info: the reviewer's own words ARE the screen. Rendered verbatim, above
            everything, never summarised into our framing - we would be paraphrasing the one
            piece of text on the page that says what to actually do. */}
        {profile.status === "needs_info" && profile.info_request && (
          <div className="ob-request ex-rise" role="status">
            <div className="ex-label mb-2">A reviewer needs something else</div>
            <p className="ob-request-body">{profile.info_request}</p>
          </div>
        )}

        <header className="ob-head ex-rise" style={{ ["--d" as string]: "40ms" }}>
          <div className="ex-label">Business verification</div>
          <h1 className="ob-title">
            {profile.status === "needs_info"
              ? "Add what's been asked for"
              : "Verify your business"}
          </h1>
          <p className="ob-lede">
            Calling and texting unlock once this is approved. Everything else in your
            workspace works now, so you can set it up while you wait.
          </p>
        </header>

        <ol className="ob-steps">
          {ONBOARDING_STEPS.map((step, i) => {
            const outstanding = outstandingFor(step, missing);
            const done = outstanding.length === 0;
            const isOpen = open === step.id;
            return (
              <li
                key={step.id}
                className={cn("ob-step ex-rise", done && "is-done", isOpen && "is-open")}
                style={{ ["--d" as string]: 100 + i * 55 + "ms" }}
              >
                <button
                  type="button"
                  className="ob-step-head"
                  aria-expanded={isOpen}
                  onClick={() => setOpen(isOpen ? null : step.id)}
                >
                  <span className="ob-node" aria-hidden="true">
                    {done ? "✓" : String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="ob-step-text">
                    <span className="ob-step-title">{step.title}</span>
                    <span className="ob-step-blurb">{step.blurb}</span>
                  </span>
                  {/* Counted from the server's own list, never from our idea of what is
                      filled in - this is the same list POST /kyc/submit is judged on. */}
                  <span className={cn("ob-step-tag", done && "is-done")}>
                    {done ? "Done" : outstanding.length + " left"}
                  </span>
                </button>

                {isOpen && (
                  <div className="ob-step-body">
                    {done ? (
                      <p className="ob-done-note">
                        Nothing outstanding here. You can still change it until you submit.
                      </p>
                    ) : (
                      <>
                        <ul className="ob-todo">
                          {outstanding.map((key) => (
                            <li key={key}>{missingLabel(key)}</li>
                          ))}
                        </ul>
                        <AuthButton type="button" onClick={() => onOpen(step.id)}>
                          {step.id === "identity" ? "Start the ID check" : "Fill this in"}
                        </AuthButton>
                      </>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>

        <div className="ob-submit ex-rise" style={{ ["--d" as string]: "460ms" }}>
          {ready ? (
            <>
              <p className="ob-submit-copy">
                Everything's here. Once you submit, a reviewer looks at it - you can't edit
                while it's with them.
              </p>
              <AuthButton type="button" block onClick={() => onOpen("submit")}>
                Submit for review
              </AuthButton>
            </>
          ) : (
            <p className="ob-submit-copy">
              <span className="ob-count">{missing.length}</span>{" "}
              {missing.length === 1 ? "thing" : "things"} still to do before you can submit.
            </p>
          )}
        </div>
      </div>
    </AuthSurface>
  );
}

/* ─────────────────────────────────────────────────────── the waiting ── */

function Waiting({ profile }: { profile: KycProfile }) {
  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Business verification"
        title="With a reviewer"
        lede="A person reads every application. We'll email you when there's a decision."
      >
        <div className="space-y-4">
          <Lamp state="wait">Submitted{when(profile.submitted_at)}</Lamp>
          {/* Deliberately no estimate and no progress bar: there is no SLA in the system,
              and a bar that fills would be a claim about time we cannot make. */}
          <AuthNotice>
            Nothing to do here. Meanwhile you can invite your team, connect a provider and
            set the workspace up - calling and texting are the only things waiting on this.
          </AuthNotice>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}

/* ────────────────────────────────────────────────────── the outcomes ── */

function Approved({ profile, onContinue }: { profile: KycProfile; onContinue: () => void }) {
  const limits = profile.limits;
  const deposit = profile.deposit_required_cents;
  const hasLimits = limits != null && Object.keys(limits).length > 0;

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Business verification"
        title="You're verified"
        lede="Calling and texting are on."
      >
        <div className="space-y-4">
          <Lamp state="live">Approved{when(profile.decided_at)}</Lamp>

          {/* Stated as the limits they start on, never as "you're all done": an approved
              account can still be refused tomorrow by the spend gate or a daily cap, and
              success copy must not contradict a refusal the customer is about to meet. */}
          {(hasLimits || deposit) && (
            <div className="ob-limits">
              <div className="ex-label mb-2">What you start with</div>
              <ul>
                {hasLimits &&
                  Object.entries(limits).map(([k, v]) => (
                    <li key={k}>
                      <span>{k.replace(/_/g, " ")}</span>
                      <b>{v}</b>
                    </li>
                  ))}
                {deposit ? (
                  <li>
                    <span>deposit required</span>
                    <b>${(deposit / 100).toFixed(2)}</b>
                  </li>
                ) : null}
              </ul>
            </div>
          )}

          {/* A use-case change is the one edit allowed after approval, and it lands in
              `use_case_pending` until a reviewer accepts it. Saying so stops it reading as
              a save that did not take. */}
          {profile.use_case_pending && (
            <AuthNotice>
              Your updated description of how you'll use calling and texting is with a
              reviewer. What's approved today keeps working until they've looked at it.
            </AuthNotice>
          )}

          <AuthButton type="button" block onClick={onContinue}>
            Go to your inbox
          </AuthButton>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}

function Rejected({ profile }: { profile: KycProfile }) {
  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Business verification"
        title="We couldn't verify this business"
        lede="Calling and texting aren't available for this workspace."
      >
        <div className="space-y-4">
          {/* The reviewer's words, attributed as theirs, not reworded into ours. And NO
              retry or appeal button: no transition leaves `rejected`, so the control would
              resolve to nothing - a door painted on a wall. The email is the real route. */}
          {profile.decision_reason && (
            <blockquote className="ob-quote">
              <div className="ex-label mb-2">From the reviewer</div>
              {profile.decision_reason}
            </blockquote>
          )}
          <p className="ob-plain">
            If you think this is a mistake, reply to the email we sent - it reaches the
            people who made the decision.
          </p>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}

function Suspended() {
  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Business verification"
        title="This account is suspended"
        lede="Calling, texting and number orders are paused."
      >
        {/* No reason is claimed: the profile payload carries none for a member, and the
            suspension reason is owner-gated. Pointing at the mail is both true and the only
            useful thing we can tell a staff member looking at this screen. */}
        <AuthNotice>We've emailed the account owners with the details.</AuthNotice>
      </AuthPlate>
    </AuthSurface>
  );
}

/**
 * Who still has to repeat their ID check.
 *
 * Mirrors `kyc_tick.reverification_tick`'s `redone` test exactly, per person and negated,
 * because the obvious alternative - "anyone not currently `verified`" - is the reassuring
 * answer that can never be right here: moving a profile to `reverification_due` does NOT
 * touch person rows, so every owner is still `verified` carrying last year's `verified_at`.
 * That filter would return an EMPTY list on the one screen whose whole job is to name
 * people, and the screen would quietly offer nothing to anyone.
 *
 * `next_reverification_at` is the same field the tick reads as its `started` cutoff, and it
 * is not rewritten until re-approval - so while the profile sits in `reverification_due`,
 * client and server are reading one value, not two that happen to agree.
 */
function staleOwners(profile: KycProfile): KycPerson[] {
  const started = profile.next_reverification_at
    ? Date.parse(profile.next_reverification_at)
    : null;
  return profile.persons
    .filter((p) => p.role === "owner" || p.role === "beneficial_owner")
    .filter((p) => {
      if (p.status !== "verified") return true;
      // The tick falls back to `now` when the cutoff is missing, and no past check clears
      // that bar - so an absent cutoff means everyone is stale, not nobody.
      if (started === null) return true;
      // ...and it substitutes the cutoff itself for a missing `verified_at`, which then
      // passes its own `>=` test. Mirrored rather than "fixed": the point is to agree.
      if (!p.verified_at) return false;
      return Date.parse(p.verified_at) < started;
    });
}

function Reverification({
  profile,
  onOpen,
  onContinue,
}: {
  profile: KycProfile;
  onOpen: (hash?: string) => void;
  onContinue: () => void;
}) {
  const { me, orgId } = useAuth();
  const canEdit = hasPermission(me, orgId, "org:update");
  const stale = staleOwners(profile);

  // Three groups, because the server answers these three cases differently and one button
  // would be wrong for two of them:
  //   it's me -> I can start my own check;
  const mine = stale.filter((p) => p.is_you);
  //   linked to somebody else's account -> `not_your_identity`, nobody else may start it;
  const blocked = stale.filter((p) => !p.is_you && p.is_user);
  //   linked to no account at all -> anyone holding org:update can raise a link for them.
  const linkable = stale.filter((p) => !p.is_you && !p.is_user);

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Business verification"
        title="Time to re-check IDs"
        lede="Your workspace keeps working. This is the yearly check, not a pause."
      >
        <div className="space-y-4">
          {profile.info_request && <AuthNotice>{profile.info_request}</AuthNotice>}

          {mine.length > 0 && (
            <AuthButton type="button" block onClick={() => onOpen("identity")}>
              Re-check my ID
            </AuthButton>
          )}

          {linkable.length > 0 && canEdit && (
            <div className="space-y-2">
              <p className="ob-plain">
                {names(linkable)} {linkable.length === 1 ? "has" : "have"} no account here,
                so you can raise their ID link and send it on.
              </p>
              <AuthButton type="button" tone="quiet" block onClick={() => onOpen("identity")}>
                {linkable.length === 1 ? "Get their link" : "Get their links"}
              </AuthButton>
            </div>
          )}

          {/* Named, with no button, because there is no call this viewer could make that
              would succeed. Naming them is the only actionable thing left: it says who to
              go and ask. */}
          {blocked.length > 0 && (
            <AuthNotice>
              Waiting on {names(blocked)} to repeat{" "}
              {blocked.length === 1 ? "their own check" : "their own checks"} - only they can
              do that, from their own account.
            </AuthNotice>
          )}

          {stale.length === 0 && (
            <Lamp state="wait">Every owner has re-checked. We're confirming it now.</Lamp>
          )}

          <button type="button" className="ex-link" onClick={onContinue}>
            Back to the inbox
          </button>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}

function names(people: KycPerson[]): string {
  const list = people.map((p) => p.full_name);
  if (list.length <= 1) return list.join("");
  return list.slice(0, -1).join(", ") + " and " + list[list.length - 1];
}

function when(iso: string | null): string {
  if (!iso) return "";
  return " " + new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "long" });
}
