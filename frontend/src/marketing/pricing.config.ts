/**
 * THE one place the public website reads plans and prices from.
 *
 * Change a number here and the pricing page, plan cards, calculator, FAQ answers and the
 * chat assistant all follow. Nothing else on the site hard-codes a price.
 *
 * Status: DRAFT for partner review (2026-09-26). These plans are shown on the website
 * only; billing enforcement (seat caps, per-plan concurrency, per-number pools) is a
 * separate backend change and does not read this file.
 */

export type PlanCode = "starter" | "team" | "business";

export interface Plan {
  code: PlanCode;
  name: string;
  tagline: string;
  /** Price per phone number per month, in whole dollars. */
  pricePerNumber: { monthly: number; yearly: number };
  /** Numbers a workspace on this plan may hold. */
  numbers: { min: number; max: number };
  /** People who can sign in. Buying numbers never adds users. */
  users: { included: number; max: number };
  /** Price of one user above `users.included`, per month. null = not sold on this plan. */
  extraUserPrice: number | null;
  /** Included every month for EACH number, pooled across the workspace. null = pay as you go. */
  perNumberAllowance: { minutes: number; texts: number } | null;
  /** Live calls one number can carry at once (anti-spam: never more). */
  callsPerNumber: number;
  features: string[];
  cta: { label: string; to: string };
  highlight?: boolean;
}

/** Usage rates, charged beyond a plan's pool (and from the first minute on Starter). */
export const RATES = {
  minute: 0.012,
  text: 0.015,
  picture: 0.035,
  faxPage: 0.1,
  minuteBundle: { units: 1000, price: 10 },
  textBundle: { units: 1000, price: 13 },
} as const;

/** Where calls and texts can go. Mirrors backend destination_policy (home region only). */
export const COVERAGE = "US calling and texting to the 48 contiguous states";

/** Anti-spam ceiling on outbound calls from one number in a day (shown in fair use). */
export const DAILY_OUTBOUND_PER_NUMBER = 150;

/** Yearly billing saving shown on the toggle, derived from the Team plan. */
export const YEARLY_SAVING_LABEL = "Save ~17%";

export const PLANS: Plan[] = [
  {
    code: "starter",
    name: "Starter",
    tagline: "One person, one business line.",
    pricePerNumber: { monthly: 15, yearly: 12 },
    numbers: { min: 1, max: 2 },
    users: { included: 1, max: 1 },
    extraUserPrice: null,
    perNumberAllowance: null,
    callsPerNumber: 1,
    features: [
      "Calling from your browser",
      "Voicemail",
      "Shared inbox for calls and texts",
      "Texting after 10DLC approval",
      "E911 on every number",
    ],
    cta: { label: "Start with Starter", to: "/signbox?plan=starter" },
  },
  {
    code: "team",
    name: "Team",
    tagline: "A small team sharing its lines.",
    pricePerNumber: { monthly: 29, yearly: 24 },
    numbers: { min: 1, max: 10 },
    users: { included: 3, max: 6 },
    extraUserPrice: 10,
    perNumberAllowance: { minutes: 1000, texts: 500 },
    callsPerNumber: 2,
    features: [
      "Everything in Starter",
      "Team notes and assignments",
      "Call recording",
      "Phone menus and call queues",
      "Reports",
      "Integrations, webhooks and API",
      "Number porting",
    ],
    cta: { label: "Start with Team", to: "/signbox?plan=team" },
    highlight: true,
  },
  {
    code: "business",
    name: "Business",
    tagline: "Teams that live on the phone.",
    pricePerNumber: { monthly: 59, yearly: 49 },
    numbers: { min: 2, max: 50 },
    users: { included: 8, max: 25 },
    extraUserPrice: 10,
    perNumberAllowance: { minutes: 3000, texts: 1500 },
    callsPerNumber: 2,
    features: [
      "Everything in Team",
      "AI voice agent (AI minutes metered)",
      "AI text replies",
      "Power dialer",
      "Fax",
      "Admin roles and audit log",
      "Priority support",
    ],
    cta: { label: "Talk to sales", to: "/sales?plan=business" },
  },
];

export type Billing = "monthly" | "yearly";

export function planByCode(code: PlanCode): Plan {
  const plan = PLANS.find(p => p.code === code);
  if (!plan) throw new Error(`Unknown plan ${code}`);
  return plan;
}

/** The lowest monthly bill for a plan: its minimum numbers at the chosen billing. */
export function startingPrice(plan: Plan, billing: Billing): number {
  return plan.numbers.min * plan.pricePerNumber[billing];
}

export interface Quote {
  plan: Plan;
  numbers: number;
  users: number;
  extraUsers: number;
  monthly: number;
  poolMinutes: number | null;
  poolTexts: number | null;
  callsAtOnce: number;
  /** Why this plan cannot serve the request (too many users/numbers), if it cannot. */
  blocked: string | null;
}

/**
 * What a workspace with `users` people and `numbers` lines pays on `plan`.
 * Numbers below the plan minimum are raised to it; users above the plan maximum block.
 */
export function quote(plan: Plan, users: number, numbers: number, billing: Billing): Quote {
  const n = Math.max(numbers, plan.numbers.min);
  const extraUsers = Math.max(0, users - plan.users.included);
  let blocked: string | null = null;
  if (users > plan.users.max) blocked = `${plan.name} allows up to ${plan.users.max} ${plan.users.max === 1 ? "user" : "users"}`;
  else if (n > plan.numbers.max) blocked = `${plan.name} allows up to ${plan.numbers.max} numbers`;
  const monthly = n * plan.pricePerNumber[billing] + extraUsers * (plan.extraUserPrice ?? 0);
  return {
    plan,
    numbers: n,
    users,
    extraUsers,
    monthly,
    poolMinutes: plan.perNumberAllowance ? plan.perNumberAllowance.minutes * n : null,
    poolTexts: plan.perNumberAllowance ? plan.perNumberAllowance.texts * n : null,
    callsAtOnce: Math.min(users, n * plan.callsPerNumber),
    blocked,
  };
}

/** The cheapest plan that fits, preferring the first plan in order on ties. */
export function recommend(users: number, numbers: number, billing: Billing): Quote {
  const fits = PLANS.map(p => quote(p, users, numbers, billing)).filter(q => !q.blocked);
  if (fits.length === 0) return quote(PLANS[PLANS.length - 1], users, numbers, billing);
  return fits.reduce((best, q) => (q.monthly < best.monthly ? q : best));
}

/**
 * Competitor list prices per user per month (Sep 2026) for the calculator.
 * Re-check on each vendor's live pricing page before changing, and keep the date.
 */
export const COMPETITORS_CHECKED = "September 2026";
export const COMPETITOR_SEAT_PRICES = [
  { name: "Quo Business", monthly: 33, yearly: 23, minSeats: 1 },
  { name: "CallHippo Professional", monthly: 30, yearly: 30, minSeats: 1 },
  { name: "Aircall Essentials", monthly: 40, yearly: 30, minSeats: 3 },
  { name: "KrispCall Standard", monthly: 40, yearly: 32, minSeats: 1 },
] as const;

export function money(value: number): string {
  return Number.isInteger(value) ? `$${value.toLocaleString("en-US")}` : `$${value.toFixed(2)}`;
}

export function cents(value: number): string {
  return `${+(value * 100).toFixed(2)}¢`;
}
