/**
 * The ordered journey — a UI construct over an API that has no order.
 *
 * Every KYC section is an independent write against a `draft` profile, accepted in any
 * order, any number of times; the only gate is `missing_for_submission` at
 * `POST /kyc/submit`. So this file decides PRESENTATION ORDER and nothing else. No step is
 * ever locked, because locking would assert a backend rule that does not exist — and two
 * browser tabs would disprove it.
 *
 * A step's completeness is the server's answer, not ours: a step is done when none of its
 * `missing` keys are in the profile's `missing` array. We never recompute that. `missing`
 * is the same list `POST /kyc/submit` is judged on, and a second implementation in the
 * browser is a rule written twice and true in one place.
 *
 * `missing` is only populated while the status is `draft` or `needs_info`. In every other
 * status it is `[]` — which is a fact about the payload and NOT evidence of completeness,
 * so the stepper is only ever mounted in those two statuses.
 */

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
    blurb: "A photo of an ID and a selfie for each owner, handled by Stripe. We never see or store the images.",
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

/** Outstanding `missing` keys for one step. Empty means the SERVER considers it done. */
export function outstandingFor(step: OnboardingStep, missing: string[]): string[] {
  return missing.filter(step.owns);
}

/**
 * The step to open first: the earliest with outstanding work. Returns null when nothing is
 * outstanding — the caller shows "ready to submit" rather than a seventh step, because
 * submitting is an act, not a form.
 */
export function firstIncomplete(missing: string[]): StepId | null {
  const step = ONBOARDING_STEPS.find((s) => outstandingFor(s, missing).length > 0);
  return step ? step.id : null;
}
