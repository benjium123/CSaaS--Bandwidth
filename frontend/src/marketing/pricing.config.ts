/**
 * THE one place the public website reads plans and prices from.
 *
 * Change a number here and the pricing page, plan cards, calculator, FAQ answers and the
 * chat assistant all follow. Nothing else on the site hard-codes a price.
 *
 * Model (agreed 2026-09-26): each plan is a monthly package that includes a set number of
 * users and phone numbers; more users and numbers are add-ons. Team and Business include a
 * fixed pool of call minutes the whole workspace shares (add-on users add none); Starter is
 * pay as you go. Calls past the pool and all texts use the published rates below.
 *
 * Billing is enforced by backend services/plan_billing.py, which does not read this file:
 * keep the two in step.
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
  /** Call minutes a month shared by the workspace (0 = pay as you go). null = custom. */
  minutes: number | null;
  /** Most users the plan can have, add-ons included. null = no limit. */
  maxUsers: number | null;
  features: string[];
  cta: { label: string; to: string };
  highlight?: boolean;
}

/** Usage rates: calls past the minute pool, and every text. */
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

export type Billing = "month" | "year";

/** Paying yearly buys twelve months for the price of ten (backend MONTHS_BILLED_PER_YEAR). */
export const YEARLY = { monthsBilled: 10, label: "2 months free" } as const;

/** A monthly amount as billed: itself, or ten months once a year. */
export function perBill(monthly: number, billing: Billing): number {
  return billing === "year" ? monthly * YEARLY.monthsBilled : monthly;
}

/** What a monthly amount works out to per month when paid yearly (e.g. $45 -> $37.50). */
export function monthlyWhenYearly(monthly: number): number {
  return Math.round((monthly * YEARLY.monthsBilled * 100) / 12) / 100;
}

/** "Up to 5 users" / "Unlimited users". */
export function userLimitLine(plan: Plan): string {
  if (plan.code === "custom") return "Any number of users";
  return plan.maxUsers === null ? "Unlimited users" : `Up to ${plan.maxUsers} users`;
}

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
    minutes: 0,
    maxUsers: 5,
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
    minutes: 200,
    maxUsers: 15,
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
    extraUser: 12,
    extraNumber: 5,
    minutes: 1000,
    maxUsers: null,
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
    minutes: null,
    maxUsers: null,
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
  const fits = PRICED_PLANS.filter(p => p.maxUsers === null || users <= p.maxUsers);
  return fits.map(p => quote(p, users, numbers)).reduce((best, q) =>
    (q.monthly ?? Infinity) <= (best.monthly ?? Infinity) ? q : best,
  );
}

/** "1,000 call minutes a month, shared", or "Calls pay as you go" on Starter. */
export function minutesLine(plan: Plan): string {
  if (plan.minutes === null) return "Call minutes to fit your team";
  if (plan.minutes === 0) return "Calls pay as you go";
  return `${plan.minutes.toLocaleString("en-US")} call minutes a month, shared`;
}

/** "Team 200 and Business 1,000" for copy that names every plan's pool. */
export function minutePoolsLine(): string {
  const pools = PLANS.filter(p => p.minutes).map(p => `${p.name} ${(p.minutes ?? 0).toLocaleString("en-US")}`);
  return pools.join(" and ");
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
 * Competitor list prices per user per month for the calculator and compare pages, from each
 * vendor's pricing page (September 2026). ``yearly`` is the per-user monthly price when paid
 * yearly, null when the vendor does not publish one. Each seat includes ``numbersPerSeat``
 * numbers; more cost ``extraNumber`` a month each, null when the vendor does not publish it
 * (then extra numbers are left out of their total and the footnote says so).
 * Re-check on each vendor's live pricing page before changing, and keep the date.
 */
export const COMPETITORS_CHECKED = "September 2026";
export interface CompetitorPrice {
  slug: string;
  name: string;
  monthly: number;
  yearly: number | null;
  minSeats: number;
  numbersPerSeat: number;
  extraNumber: number | null;
}
export const COMPETITOR_SEAT_PRICES: CompetitorPrice[] = [
  { slug: "quo", name: "Quo Business", monthly: 33, yearly: 23, minSeats: 1, numbersPerSeat: 1, extraNumber: 5 },
  { slug: "callhippo", name: "CallHippo Professional", monthly: 30, yearly: null, minSeats: 1, numbersPerSeat: 1, extraNumber: null },
  { slug: "aircall", name: "Aircall Essentials", monthly: 30, yearly: 22.5, minSeats: 3, numbersPerSeat: 1, extraNumber: 6 },
  { slug: "krispcall", name: "KrispCall Standard", monthly: 40, yearly: null, minSeats: 1, numbersPerSeat: 1, extraNumber: null },
  { slug: "ringcentral", name: "RingCentral Core", monthly: 30, yearly: 20, minSeats: 1, numbersPerSeat: 1, extraNumber: null },
];

/** A competitor's monthly cost for the same team: seats (with their minimum) plus the
 * numbers beyond what the seats include. Yearly uses their yearly seat price when published. */
export function competitorCost(c: CompetitorPrice, users: number, numbers: number, billing: Billing = "month"): number {
  const seats = Math.max(users, c.minSeats);
  const seat = billing === "year" && c.yearly !== null ? c.yearly : c.monthly;
  const extraNumbers = Math.max(numbers - seats * c.numbersPerSeat, 0);
  return seats * seat + extraNumbers * (c.extraNumber ?? 0);
}

/** Whether a competitor's total leaves out extra numbers it does not publish a price for. */
export function missesNumberPrice(c: CompetitorPrice, users: number, numbers: number): boolean {
  return c.extraNumber === null && numbers > Math.max(users, c.minSeats) * c.numbersPerSeat;
}

export function money(value: number): string {
  return Number.isInteger(value) ? `$${value.toLocaleString("en-US")}` : `$${value.toFixed(2)}`;
}

export function cents(value: number): string {
  return `${+(value * 100).toFixed(2)}¢`;
}

/**
 * The full feature comparison on /pricing. Values are in PLANS order (Starter, Team,
 * Business, Custom): true = included, false = not on that plan, a string = the detail.
 * Only list what the product does today; every row here is a promise.
 */
export type MatrixValue = boolean | string;
export interface MatrixRow {
  label: string;
  note?: string;
  values: MatrixValue[] | ((billing: Billing) => MatrixValue[]);
}
export interface MatrixSection { title: string; rows: MatrixRow[] }

const byPlan = (fn: (plan: Plan) => MatrixValue): MatrixValue[] => PLANS.map(fn);
const perPeriod = (billing: Billing) => (billing === "year" ? "/yr" : "/mo");

export const FEATURE_SECTIONS: MatrixSection[] = [
  {
    title: "Plan",
    rows: [
      { label: "Users included", values: byPlan(p => (p.included ? String(p.included.users) : "Custom")) },
      { label: "User limit", values: byPlan(p => (p.code === "custom" ? "Custom" : p.maxUsers === null ? "Unlimited" : `Up to ${p.maxUsers}`)) },
      { label: "Phone numbers included", values: byPlan(p => (p.included ? String(p.included.numbers) : "Custom")) },
      { label: "Extra user", note: "Add-on", values: b => byPlan(p => (p.extraUser === null ? "Custom" : `${money(perBill(p.extraUser, b))}${perPeriod(b)}`)) },
      { label: "Extra phone number", note: "Add-on", values: b => byPlan(p => (p.extraNumber === null ? "Custom" : `${money(perBill(p.extraNumber, b))}${perPeriod(b)}`)) },
      { label: "Pay yearly", note: YEARLY.label, values: byPlan(p => p.price !== null || "Custom") },
    ],
  },
  {
    title: "Calling",
    rows: [
      { label: "Call minutes included", note: "Shared by the whole team, every month", values: byPlan(p => (p.minutes === null ? "Custom" : p.minutes === 0 ? "Pay as you go" : p.minutes.toLocaleString("en-US"))) },
      { label: "Calls after that", values: byPlan(p => (p.price === null ? "Volume rates" : `${cents(RATES.minute)}/min`)) },
      { label: "Coverage", values: byPlan(() => "48 US states") },
      { label: "Live calls per number", values: byPlan(() => String(CALLS_PER_NUMBER)) },
      { label: "Calling from your browser", values: [true, true, true, true] },
      { label: "Voicemail", values: [true, true, true, true] },
      { label: "Call recording", values: [false, true, true, true] },
      { label: "Phone menus and call queues", values: [false, true, true, true] },
      { label: "Power dialer", values: [false, false, true, true] },
      { label: "AI voice agent", note: "AI minutes metered", values: [false, false, true, true] },
    ],
  },
  {
    title: "Messaging",
    rows: [
      { label: "Texts", note: "Registration required", values: byPlan(p => (p.price === null ? "Volume rates" : `${cents(RATES.text)} each`)) },
      { label: "Picture messages", values: byPlan(p => (p.price === null ? "Volume rates" : `${cents(RATES.picture)} each`)) },
      { label: "Texting registration (10DLC)", note: "Done inside the app", values: [true, true, true, true] },
      { label: "Shared inbox for calls and texts", values: [true, true, true, true] },
      { label: "AI text replies", values: [false, false, true, true] },
      { label: "Fax", values: [false, false, true, true] },
    ],
  },
  {
    title: "Numbers",
    rows: [
      { label: "Local business numbers", values: [true, true, true, true] },
      { label: "E911 on every number", values: [true, true, true, true] },
      { label: "Port numbers from other carriers", values: [false, true, true, true] },
      { label: "Help moving all your numbers", values: [false, false, false, true] },
    ],
  },
  {
    title: "Team and admin",
    rows: [
      { label: "Team notes and assignments", values: [false, true, true, true] },
      { label: "Reports", values: [false, true, true, true] },
      { label: "Admin roles and audit log", values: [false, false, true, true] },
      { label: "Integrations, webhooks and API", values: [false, true, true, true] },
    ],
  },
  {
    title: "Support",
    rows: [
      { label: "Help by chat and email", values: [true, true, true, true] },
      { label: "Priority support", values: [false, false, true, true] },
      { label: "Custom onboarding", values: [false, false, false, true] },
      { label: "A named contact at Ringlite", values: [false, false, false, true] },
    ],
  },
];
