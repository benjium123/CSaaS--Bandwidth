/**
 * THE one place the public website reads plans and prices from.
 *
 * Change a number here and the pricing page, plan cards, calculator, FAQ answers and the
 * chat assistant all follow. Nothing else on the site hard-codes a price.
 *
 * Model (agreed 2026-09-26): each plan is a monthly package that includes a set number of
 * users and phone numbers; more users and numbers are add-ons. Calls and texts are pay as
 * you go on every plan, at the published rates below.
 *
 * Billing enforcement (seat and number caps per plan) is a separate backend change and does
 * not read this file.
 */

export type PlanCode = "starter" | "team" | "business" | "custom";

export interface Plan {
  code: PlanCode;
  name: string;
  tagline: string;
  /** Monthly package price in whole dollars. null = custom quote. */
  price: number | null;
  /** Users and phone numbers the package includes. null = set per customer. */
  included: { users: number; numbers: number } | null;
  /** Monthly price of each user / number above the package. null = not sold on this plan. */
  extraUser: number | null;
  extraNumber: number | null;
  features: string[];
  cta: { label: string; to: string };
  highlight?: boolean;
}

/** Usage rates. Every plan pays for calls and texts as it goes. */
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

/** Live calls one number carries at once (anti-spam; add numbers to add capacity). */
export const CALLS_PER_NUMBER = 2;

/** Anti-spam ceiling on outbound calls from one number in a day (shown in fair use). */
export const DAILY_OUTBOUND_PER_NUMBER = 150;

/** Above this many users the calculator points to the Custom plan. */
export const CUSTOM_FROM_USERS = 50;

export const PLANS: Plan[] = [
  {
    code: "starter",
    name: "Starter",
    tagline: "One person, one business line.",
    price: 15,
    included: { users: 1, numbers: 1 },
    extraUser: 15,
    extraNumber: 5,
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
    tagline: "A small team, a line each.",
    price: 45,
    included: { users: 3, numbers: 3 },
    extraUser: 15,
    extraNumber: 5,
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
    tagline: "Ten people on the phone all day.",
    price: 130,
    included: { users: 10, numbers: 10 },
    extraUser: 10,
    extraNumber: 5,
    features: [
      "Everything in Team",
      "AI voice agent (AI minutes metered)",
      "AI text replies",
      "Power dialer",
      "Fax",
      "Admin roles and audit log",
      "Priority support",
    ],
    cta: { label: "Start with Business", to: "/signbox?plan=business" },
  },
  {
    code: "custom",
    name: "Custom",
    tagline: "Bigger teams and special setups.",
    price: null,
    included: null,
    extraUser: null,
    extraNumber: null,
    features: [
      "Everything in Business",
      "Volume pricing on users, numbers and usage",
      "Help moving all your numbers",
      "Custom onboarding",
      "A named contact at Ringlite",
    ],
    cta: { label: "Talk to sales", to: "/sales?plan=custom" },
  },
];

/** Plans with a published price (everything but Custom). */
export const PRICED_PLANS = PLANS.filter(plan => plan.price !== null);

export function planByCode(code: PlanCode): Plan {
  const plan = PLANS.find(p => p.code === code);
  if (!plan) throw new Error(`Unknown plan ${code}`);
  return plan;
}

export interface Quote {
  plan: Plan;
  users: number;
  numbers: number;
  extraUsers: number;
  extraNumbers: number;
  /** Monthly total before usage. null for Custom. */
  monthly: number | null;
  callsAtOnce: number;
}

/** What a workspace with `users` people and `numbers` lines pays on `plan` each month. */
export function quote(plan: Plan, users: number, numbers: number): Quote {
  const inc = plan.included ?? { users, numbers };
  const extraUsers = Math.max(0, users - inc.users);
  const extraNumbers = Math.max(0, numbers - inc.numbers);
  const monthly =
    plan.price === null
      ? null
      : plan.price + extraUsers * (plan.extraUser ?? 0) + extraNumbers * (plan.extraNumber ?? 0);
  const lines = Math.max(numbers, inc.numbers);
  return {
    plan,
    users,
    numbers: lines,
    extraUsers,
    extraNumbers,
    monthly,
    callsAtOnce: Math.min(users, lines * CALLS_PER_NUMBER),
  };
}

/**
 * The cheapest priced plan for this team, preferring the higher plan on a tie (it has more
 * features). Past CUSTOM_FROM_USERS users, the Custom plan.
 */
export function recommend(users: number, numbers: number): Quote {
  if (users > CUSTOM_FROM_USERS) return quote(planByCode("custom"), users, numbers);
  return PRICED_PLANS.map(p => quote(p, users, numbers)).reduce((best, q) =>
    (q.monthly ?? Infinity) <= (best.monthly ?? Infinity) ? q : best,
  );
}

/** "3 users, 3 numbers" style summary of a package. */
export function packageLine(plan: Plan): string {
  if (!plan.included) return "Users and numbers to fit your team";
  const { users, numbers } = plan.included;
  return `${users} ${users === 1 ? "user" : "users"} + ${numbers} ${numbers === 1 ? "number" : "numbers"} included`;
}

/** "Extra user $15 · extra number $5" style summary of a plan's add-ons. */
export function addOnLine(plan: Plan): string | null {
  if (plan.extraUser === null || plan.extraNumber === null) return null;
  return `Extra user ${money(plan.extraUser)} · extra number ${money(plan.extraNumber)}`;
}

/**
 * Competitor list prices per user per month (monthly billing, Sep 2026) for the calculator.
 * Re-check on each vendor's live pricing page before changing, and keep the date.
 */
export const COMPETITORS_CHECKED = "September 2026";
export const COMPETITOR_SEAT_PRICES = [
  { name: "Quo Business", monthly: 33, minSeats: 1 },
  { name: "CallHippo Professional", monthly: 30, minSeats: 1 },
  { name: "Aircall Essentials", monthly: 40, minSeats: 3 },
  { name: "KrispCall Standard", monthly: 40, minSeats: 1 },
] as const;

export function money(value: number): string {
  return Number.isInteger(value) ? `$${value.toLocaleString("en-US")}` : `$${value.toFixed(2)}`;
}

export function cents(value: number): string {
  return `${+(value * 100).toFixed(2)}¢`;
}
