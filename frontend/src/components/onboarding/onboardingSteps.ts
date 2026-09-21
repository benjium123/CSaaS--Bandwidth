/**
 * The ordered journey — a UI construct over an API that has no order.
 *
 * Every KYC section is an independent write against a `draft` profile, accepted in any
 * order, any number of times; the only gate is `missing_for_submission` at
 * `POST /kyc/submit`. So this file decides PRESENTATION ORDER and nothing else. No step is
 * ever locked, because locking would assert a backend rule that does not exist — and two
 * browser tabs would disprove it.
 *
 * A step's outstanding work is the server's answer, not ours: its `missing` keys, filtered.
 * We never recompute what is filled in - `missing` is the same list `POST /kyc/submit` is
 * judged on, and a second implementation in the browser is a rule written twice and true in
 * one place. But an EMPTY filter is not automatically a completion, because `missing`
 * under-reports the per-owner keys; see `stepStateFor`.
 *
 * `missing` is only populated while the status is `draft` or `needs_info`. In every other
 * status it is `[]` — which is a fact about the payload and NOT evidence of completeness,
 * so the stepper is only ever mounted in those two statuses.
 */

export type AccountType = "business" | "individual";

export type StepId = "business" | "use_case" | "owners" | "identity" | "documents" | "agreement";

export type OnboardingStep = {
  id: StepId;
  title: string;
  /** Why we ask — shown under the title. Never a promise about the outcome. */
  blurb: string;
  /**
   * The `missing` keys this step is responsible for. `use_case.*` is matched by prefix
   * because the server emits one key per unfilled field.
   */
  owns: (key: string) => boolean;
};

const BUSINESS_KEYS = new Set([
  "country",
  "legal_name",
  "entity_type",
  "registration_number",
  "registered_address",
  "website",
  "business_email",
  "business_phone",
]);

/**
 * The personal-details keys an individual profile owns. Same shape as the business set,
 * minus the entity/registration fields that only exist for a company, and with the
 * personal contact fields the server emits for an individual.
 */
const INDIVIDUAL_KEYS = new Set([
  "country",
  "legal_name",
  "business_email",
  "business_phone",
]);

export const ONBOARDING_STEPS: OnboardingStep[] = [
  {
    id: "business",
    title: "Your business",
    blurb: "The registered details, as they appear on your incorporation documents.",
    owns: (k) => BUSINESS_KEYS.has(k),
  },
  {
    id: "use_case",
    title: "How you'll use it",
    blurb:
      "Worth being specific: this is what we compare your traffic against later, so a vague answer here means more false alarms for you afterwards.",
    owns: (k) => k.startsWith("use_case."),
  },
  {
    id: "owners",
    title: "Owners",
    blurb: "Anyone who owns a meaningful share of the business, and where they live now.",
    owns: (k) => k === "owner" || k === "residential_address",
  },
  {
    id: "identity",
    title: "Prove it's you",
    blurb: "A photo of an ID and a selfie for each owner, handled by our identity provider. We never see or store the images.",
    owns: (k) => k === "id_verification",
  },
  {
    id: "documents",
    title: "Documents",
    blurb: "One document proving the business exists, plus a recent proof of address for each owner.",
    owns: (k) => k === "documents" || k === "proof_of_address",
  },
  {
    id: "agreement",
    title: "Agreement",
    blurb: "The rules for what you may send and how you collect consent.",
    owns: (k) => k === "agreement",
  },
];

/**
 * The individual journey. Same step IDs, so the page's switch and the stepper's state
 * machine are unchanged; what differs is which keys each step owns and what it says.
 *
 * There is deliberately NO documents step and NO ownership/residence requirement: an
 * individual is not asked to prove a company exists, and the server does not emit
 * `documents`, `proof_of_address` or `residential_address` for them. Rendering those steps
 * would be a form the submit gate never reads.
 *
 * The applicant step reuses the `owners` id so the existing owner-count gate in
 * `stepStateFor` applies unchanged: an individual with no person row is `waiting`, not
 * `done`, and the ID step cannot be read as complete out of a silence.
 */
export const INDIVIDUAL_ONBOARDING_STEPS: OnboardingStep[] = [
  {
    id: "business",
    title: "Your details",
    blurb: "Your legal name and how we can reach you. These are the details we check against your ID.",
    owns: (k) => INDIVIDUAL_KEYS.has(k),
  },
  {
    id: "use_case",
    title: "How you'll use it",
    blurb:
      "Worth being specific: this is what we compare your traffic against later, so a vague answer here means more false alarms for you afterwards.",
    owns: (k) => k.startsWith("use_case."),
  },
  {
    id: "owners",
    title: "You",
    blurb: "The applicant on this account. We check your ID against these details.",
    owns: (k) => k === "owner",
  },
  {
    id: "identity",
    title: "Prove it's you",
    blurb: "A photo of your ID and a selfie, handled by Didit. We never see or store the images.",
    owns: (k) => k === "id_verification",
  },
  {
    id: "agreement",
    title: "Agreement",
    blurb: "The rules for calling and how you collect consent.",
    owns: (k) => k === "agreement",
  },
];

/** The step list for an account type. Defaults to business so existing callers are unchanged. */
export function stepsFor(accountType: AccountType = "business"): OnboardingStep[] {
  return accountType === "individual" ? INDIVIDUAL_ONBOARDING_STEPS : ONBOARDING_STEPS;
}

/** Outstanding `missing` keys for one step. NOT on its own a verdict - see `stepStateFor`. */
export function outstandingFor(step: OnboardingStep, missing: string[]): string[] {
  return missing.filter(step.owns);
}

/**
 * THE THIRD STATE, and the reason it has to exist.
 *
 * `missing` under-reports. Three of its keys - `id_verification`, `proof_of_address`,
 * `residential_address` - are PER OWNER, so the server cannot emit them until an owner
 * person row exists. A brand-new profile comes back with eighteen keys and none of those
 * three: there is nobody to check, not somebody who has been checked.
 *
 * So for those steps an ABSENCE is not evidence. Reading it as one is how "Prove it's you"
 * came to render a green tick and the word "Done" on a workspace that had done nothing - a
 * claim about an identity check that had never been started, made out of a silence.
 *
 * A step may therefore only reach `done` on POSITIVE evidence: the server had the chance to
 * name the key as outstanding and did not. Where that chance never arose the state is
 * `waiting`, which is neither a tick nor a count, and says what it is waiting on.
 *
 * `business`, `use_case` and `agreement` are exempt because they always apply: those keys
 * are computed for every profile in `draft`/`needs_info`, so there an absence IS evidence.
 */
export type StepState =
  | { kind: "done" }
  | { kind: "todo"; outstanding: string[] }
  /** `outstanding` may still hold keys - a waiting step can have assessable work too. */
  | { kind: "waiting"; label: string; outstanding: string[] };

const NEEDS_AN_OWNER = "Add an owner first";

/** Owners and beneficial owners - the roles the per-owner `missing` keys are computed for. */
export function ownerCount(persons: { role: string }[]): number {
  return persons.filter((p) => p.role === "owner" || p.role === "beneficial_owner").length;
}

export function stepStateFor(step: OnboardingStep, missing: string[], owners: number): StepState {
  const outstanding = outstandingFor(step, missing);

  switch (step.id) {
    // Nothing to check until there is somebody to check. With no owners the server emits no
    // `id_verification` at all, so `done` here could only ever be read out of a silence.
    case "identity":
      if (owners === 0) return { kind: "waiting", label: NEEDS_AN_OWNER, outstanding };
      break;

    // Half of this step - the proof of address - is per owner. The business document can be
    // uploaded now and is still reported, but the step cannot be called done while the other
    // half is unassessable.
    case "documents":
      if (owners === 0) return { kind: "waiting", label: NEEDS_AN_OWNER, outstanding };
      break;

    // `done` needs an owner to exist, not merely the absence of the `owner` key.
    case "owners":
      if (outstanding.length > 0) return { kind: "todo", outstanding };
      if (owners === 0) return { kind: "waiting", label: NEEDS_AN_OWNER, outstanding };
      return { kind: "done" };

    default:
      break;
  }

  return outstanding.length > 0 ? { kind: "todo", outstanding } : { kind: "done" };
}

/**
 * The step to open first: the earliest with outstanding work. Returns null when nothing is
 * outstanding — the caller shows "ready to submit" rather than a seventh step, because
 * submitting is an act, not a form.
 */
export function firstIncomplete(missing: string[], accountType: AccountType = "business"): StepId | null {
  const step = stepsFor(accountType).find((s) => outstandingFor(s, missing).length > 0);
  return step ? step.id : null;
}
