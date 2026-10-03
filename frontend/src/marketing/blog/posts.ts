import { ALTERNATIVE_SLUGS, COMPETITORS } from "@/marketing/content";
import {
  CALLS_PER_NUMBER,
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  COVERAGE,
  DAILY_OUTBOUND_PER_NUMBER,
  PLANS,
  RATES,
  YEARLY,
  cents,
  competitorCost,
  competitorTier,
  minutePoolsLine,
  missesNumberPrice,
  money,
  monthlyWhenYearly,
  planByCode,
  recommend,
  type CompetitorPrice,
} from "@/marketing/pricing.config";

export type Block =
  | { kind: "p"; text: string }
  | { kind: "h2"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "table"; head: string[]; rows: string[][]; caption?: string }
  | { kind: "cta"; text: string; label: string; to: string };

export interface Post {
  slug: string;
  title: string;
  description: string;
  published: string;
  updated?: string;
  topic: "Texting and 10DLC" | "Pricing" | "Switching";
  readMinutes: number;
  draft: boolean;
  blocks: Block[];
}

/** Sizes used by the cost tables: people and numbers grow together. */
const SIZES = [3, 5, 10, 15] as const;

/** A price we print; null prices read as a custom quote rather than a number we do not have. */
function priceLabel(price: number | null): string {
  return price === null ? "a custom quote" : money(price);
}

/** Recommended Ringlite monthly total for a team of `users` people with one number each. */
function ringliteMonthly(users: number): number {
  const monthly = recommend(users, users).monthly;
  if (monthly === null) throw new Error(`No published monthly price for ${users} people`);
  return monthly;
}

function ringliteCell(users: number): string {
  const q = recommend(users, users);
  return `${q.plan.name} at ${priceLabel(q.monthly)}`;
}

function competitorCell(c: CompetitorPrice, users: number): string {
  return `${competitorTier(c, users).name} at ${money(competitorCost(c, users, users))}`;
}

function competitorBySlug(slug: string): CompetitorPrice {
  const found = COMPETITOR_SEAT_PRICES.find(c => c.slug === slug);
  if (!found) throw new Error(`Unknown competitor ${slug}`);
  return found;
}

const QUO = competitorBySlug("quo");

const COST_TABLE: Block = {
  kind: "table",
  head: ["People and numbers", "Ringlite", ...COMPETITOR_SEAT_PRICES.map(c => c.name)],
  rows: SIZES.map(n => [String(n), ringliteCell(n), ...COMPETITOR_SEAT_PRICES.map(c => competitorCell(c, n))]),
  caption: `Monthly list prices for the same team size, checked ${COMPETITORS_CHECKED}. Ringlite is the package we recommend at that size; competitor totals use each vendor's published seat price, include the numbers its seats carry, and add extra numbers only where the vendor publishes a price for them. Taxes and usage are not included.`,
};

const QUO_TABLE: Block = {
  kind: "table",
  head: ["People and numbers", "Ringlite monthly", "Ringlite yearly", "Quo monthly", "Quo yearly"],
  rows: ([3, 5, 10] as const).map(n => [
    String(n),
    money(ringliteMonthly(n)),
    money(monthlyWhenYearly(ringliteMonthly(n))),
    money(competitorCost(QUO, n, n, "month")),
    money(competitorCost(QUO, n, n, "year")),
  ]),
  caption: `Per month. Ringlite yearly figures are the monthly equivalent when you pay for a year up front; Quo figures use its published seat prices with one number per seat, checked ${COMPETITORS_CHECKED}. Taxes and usage are not included.`,
};

const WHAT_IS_10DLC: Post = {
  slug: "what-is-10dlc",
  title: "What is 10DLC, and does my business need it?",
  description:
    "10DLC is how US carriers register business texting. Here is who needs it, what brand and campaign approval involves, and how to get started today.",
  published: "2026-10-03",
  topic: "Texting and 10DLC",
  readMinutes: 7,
  draft: false,
  blocks: [
    {
      kind: "p",
      text: "10DLC stands for 10-digit long code. It is the ordinary ten-digit number your customers already know, the same kind you would print on a business card, used for business texting instead of for calls from a person's handset. The acronym describes the number, but what it really names is a registration: US carriers want to know who is behind the messages before they let them through.",
    },
    {
      kind: "p",
      text: "If your team sends appointment reminders, order updates or follow-ups from a business number, this registration applies to you. Here is what it involves and how to get through it without a rejected application.",
    },
    { kind: "h2", text: "What 10DLC means" },
    {
      kind: "p",
      text: "Carriers split phone traffic into two groups. Person-to-person, or P2P, is one human texting another from a handset. Application-to-person, or A2P, is a business sending messages from software. A2P traffic from a ten-digit local number is what 10DLC covers. The split matters because the two are filtered differently: P2P messages pass through, while A2P messages from an unregistered sender do not.",
    },
    {
      kind: "p",
      text: "The registration runs through The Campaign Registry, the registry US carriers use to keep a record of who is texting and why. You do not open an account there yourself. Your messaging provider submits your details on your behalf, and Ringlite's brand and campaign forms are how you supply them.",
    },
    { kind: "h2", text: "Who needs to register" },
    {
      kind: "p",
      text: "Any business texting customers from a local US number needs a registered brand and campaign before its messages will be delivered. That is true whether you send a hundred texts a month or a hundred thousand. If the number is a Ringlite number and the messages go to customers, registration applies.",
    },
    {
      kind: "p",
      text: "Personal texting from your own phone is not part of this. The line is whether a business is sending messages to customers through a messaging platform.",
    },
    { kind: "h2", text: "Brand and campaign are two registrations, not one" },
    {
      kind: "p",
      text: "A brand is your business: legal name, business address, a tax ID or equivalent, and details that have to line up with public records. A campaign is what you plan to send: the kind of messages, who receives them, and how those people agreed to hear from you.",
    },
    {
      kind: "p",
      text: "Both have to be approved, and a brand on its own lets you send nothing. You need an approved campaign attached to the brand before a single message goes out. This is the step people underestimate. Vague campaign descriptions and business details that do not match public records are the usual reasons a submission comes back for changes.",
    },
    {
      kind: "p",
      text: "Think of the campaign as a promise about what your texts will be. If you register for appointment reminders and later start promotional blasts from the same number, you have broken that promise, and the filtering that follows is the consequence.",
    },
    { kind: "h2", text: "What happens if you skip it" },
    {
      kind: "p",
      text: "Unregistered messages are filtered or blocked by the carriers. They are not queued for review and there is nothing to appeal afterwards. For a business that relies on texts to confirm appointments or answer questions, that is a silent failure: the messages look sent on your side and never arrive on theirs.",
    },
    {
      kind: "p",
      text: "There is no reliable way around registration for local-number business texting in the US. It is not a formality you can skip by choosing a different provider.",
    },
    { kind: "h2", text: "How long approval takes" },
    {
      kind: "p",
      text: "With Ringlite, carrier approval usually takes a few days to a couple of weeks. Your account shows the status of the brand and the campaign while you wait, so you can see whether you are waiting on your own details or on the carriers. Texting turns on after carriers approve. Calling works the whole time, because registration only gates messages.",
    },
    { kind: "p", text: `Once you are approved, texts are ${cents(RATES.text)} per segment.` },
    { kind: "h2", text: "Consent and STOP do not go away" },
    {
      kind: "p",
      text: "Registration and consent are separate requirements, and passing one does not satisfy the other. The TCPA requires that people agree before you text them and that you honor a request to stop. A registered number used to text people who never opted in will still be filtered, and repeated complaints can put the whole campaign at risk.",
    },
    {
      kind: "p",
      text: "Every commercial message should identify the sender and explain how to opt out. Ringlite handles standard opt-out keywords such as STOP automatically and enforces them on every send, so a person who opts out stops receiving messages from that campaign. Unsolicited bulk texting is not allowed, and no registration makes it acceptable.",
    },
    { kind: "h2", text: "Sole proprietors" },
    {
      kind: "p",
      text: "A sole proprietor can register. The details matter more here, because there is less public record to match against, so a mismatch is easier to hit. Use the legal name and address exactly as they appear on official records, keep the use case narrow, and describe your opt-in in plain words: where customers sign up and what they are told they will receive.",
    },
    {
      kind: "p",
      text: "If a submission comes back, check the name, the address and the use case description first. Fix the detail and submit again.",
    },
    { kind: "h2", text: "A checklist before you apply" },
    {
      kind: "list",
      items: [
        "Legal business name, address and tax ID exactly as they appear on public records.",
        "The phone number or numbers you plan to text from.",
        "A specific use case: what you send and who receives it.",
        "Two or three sample messages that read the way your real ones will.",
        "A description of how customers opt in, and the place they do it.",
        "A privacy policy link, live on your site, that mentions text messaging.",
      ],
    },
    { kind: "h2", text: "How Ringlite handles the forms" },
    {
      kind: "p",
      text: "The brand and campaign forms live inside your Ringlite account. Fill them in, submit, and watch the status while carriers review. Registration carries a one-time charge plus a small monthly carrier fee, and Ringlite shows both as a single total at checkout, so there is no separate invoice later.",
    },
    {
      kind: "p",
      text: "Toll-free numbers are registered a different way, through a separate verification with its own steps. If you plan to text from a toll-free line rather than a local number, expect a different form and a different review.",
    },
    {
      kind: "cta",
      text: "Fill in the brand and campaign forms inside your account, and start sending once carriers approve.",
      label: "See texting on Ringlite",
      to: "/product/texting",
    },
  ],
};

const BUSINESS_TEXTS_NOT_DELIVERED: Post = {
  slug: "business-texts-not-delivered",
  title: "Why are my business text messages not being delivered?",
  description:
    "Seven common reasons business texts do not arrive, from pending 10DLC registration to opt-outs and unverified toll-free numbers, with a checklist.",
  published: "2026-10-03",
  topic: "Texting and 10DLC",
  readMinutes: 6,
  draft: false,
  blocks: [
    {
      kind: "p",
      text: "You hit send, the app shows the message going out, and the customer says they never received it. Business texting fails quietly: there is no bounce and no error, just a text that never lands. These are the seven causes to check, and none of them is mysterious once you know where to look.",
    },
    { kind: "p", text: "Work through them in order. The first one explains most cases on its own." },
    { kind: "h2", text: "1. Your 10DLC registration is missing or still pending" },
    {
      kind: "p",
      text: "Start here. Until carriers approve your brand and campaign, texts from a local US number are filtered or blocked. With Ringlite that approval usually takes a few days to a couple of weeks, and your account shows the status the whole time. If your registration is not approved yet and a text is not arriving, that is the entire answer. Nothing else needs fixing, and there is nothing to troubleshoot until the status changes.",
    },
    {
      kind: "p",
      text: "If you have not started registration at all, that comes first. Nothing will send from a local number until the brand and campaign are approved.",
    },
    { kind: "h2", text: "2. Your messages do not match the use case you registered" },
    {
      kind: "p",
      text: "Your campaign describes the messages you told carriers you would send: appointment reminders, order updates, quote follow-ups, and so on. Messages that fall outside that description can be filtered even though the campaign is approved. A number registered for reminders that then starts sending promotional blasts is the classic case.",
    },
    {
      kind: "p",
      text: "The fix is to register a campaign that covers what you actually send, or to use a separate number and campaign for the new use case. Filtering looks at content against the registered purpose, not just at the sender.",
    },
    { kind: "h2", text: "3. Link shorteners and links that look wrong" },
    {
      kind: "p",
      text: "Carriers and their filtering partners treat some links with suspicion. A link shortener is a common trigger because the destination stays hidden until someone clicks. Where you can, use your own domain in the message and point it at the right page, and avoid domains that redirect several times before landing somewhere. If your texts carry a link, open it yourself and check that it loads and that the domain matches your business.",
    },
    { kind: "h2", text: "4. No consent, or a recipient who opted out" },
    {
      kind: "p",
      text: "If people did not agree to receive texts, carriers can filter the messages no matter how the registration looks, and the TCPA does not allow you to send them in the first place. This is about the list, not the number.",
    },
    {
      kind: "p",
      text: "The other half of this cause is quieter: someone replied STOP earlier, the system is honoring it, and the messages have correctly stopped. An opted-out number will not receive further texts, and that is the intended behavior, not a bug. Ringlite applies opt-outs automatically on every send, so check the contact's status before assuming something is broken.",
    },
    { kind: "h2", text: "5. Sending too much from one number too fast" },
    {
      kind: "p",
      text: "A single number has a limited appetite for volume. Pushing thousands of messages through one line in a short window looks exactly like spam to a filter, and it can get the number throttled or blocked even when the content is fine. Spread large sends across several numbers, slow the pace, and keep bulk notifications separate from one-to-one conversations. This is how carrier filtering generally works, and the fix is to send at a human pace from more than one line.",
    },
    { kind: "h2", text: "6. Landlines and invalid numbers" },
    {
      kind: "p",
      text: "Texts only arrive at numbers that can receive them. A landline, a number that has been disconnected, or a mistyped digit will not deliver, and the failure is on the receiving end rather than yours. If one contact never gets your messages while everyone else does, check that number before you dig into your own setup.",
    },
    { kind: "h2", text: "7. A toll-free number without verification" },
    {
      kind: "p",
      text: "Toll-free numbers do not use the same 10DLC process. They are verified separately, and a toll-free number that has not been verified will not deliver business texts. If you text from a toll-free line, confirm that verification is complete instead of hunting for a 10DLC problem that does not apply to toll-free traffic.",
    },
    { kind: "h2", text: "A troubleshooting checklist" },
    {
      kind: "list",
      items: [
        "Check the registration status in your account. Not approved yet? Wait, or finish the forms.",
        "Read the message against the use case you registered. Does it fit?",
        "Send the same message without a link, and without a shortener, and see if it lands.",
        "Check whether the recipient opted out.",
        "Split a large send across more numbers and a slower schedule.",
        "Confirm the destination number can receive texts at all.",
        "For a toll-free number, confirm the toll-free verification is complete.",
      ],
    },
    { kind: "h2", text: "How Ringlite helps" },
    {
      kind: "p",
      text: "Ringlite shows your brand and campaign status in the account, so you can tell pending from broken at a glance, and opt-out keywords are handled for you and enforced on every send.",
    },
    {
      kind: "p",
      text: "Beyond that, the fixes are about content, consent and pace. No provider can override a carrier filter on a message that breaks the rules, and any provider that claims otherwise is selling you something that does not exist.",
    },
    { kind: "p", text: `Once your registration is approved, texts are ${cents(RATES.text)} per segment.` },
    {
      kind: "cta",
      text: "Check your registration status, and start sending the moment carriers approve.",
      label: "See texting on Ringlite",
      to: "/product/texting",
    },
  ],
};

const BUSINESS_PHONE_SYSTEM_COST: Post = {
  slug: "business-phone-system-cost",
  title: "How much does a business phone system cost in 2026?",
  description:
    "What a business phone system costs per person, how package pricing compares at 3, 5, 10 and 15 people, and the fees that sit outside the headline price.",
  published: "2026-10-03",
  topic: "Pricing",
  readMinutes: 8,
  draft: false,
  blocks: [
    {
      kind: "p",
      text: "Business phone pricing comes in two shapes, and the difference decides what you actually pay. Per-seat plans charge for each person and usually include one number with each seat. Package plans bundle a set number of users and numbers into a single monthly price and charge for anything above that bundle. Ringlite is the second kind. The two are easy to compare once you know your team size, so this guide puts real numbers side by side for teams of three, five, ten and fifteen people.",
    },
    { kind: "h2", text: "Per-seat pricing and package pricing" },
    {
      kind: "p",
      text: "With per-seat pricing, the monthly cost is the seat price multiplied by the number of people. Extra numbers beyond the ones the seats include cost more, and some vendors set a minimum number of seats. Cost scales in a straight line: more people, more money, every month, with no bundle to absorb growth.",
    },
    {
      kind: "p",
      text: "Package pricing bundles users and numbers into a plan and charges for the extras over the bundle. Ringlite's Team and Business plans include a pool of call minutes the whole workspace shares, Starter is pay as you go, and calls past the pool are billed per minute. For a team that fits a bundle, the monthly figure stays put as the team grows inside it.",
    },
    { kind: "h2", text: "What the same team pays" },
    COST_TABLE,
    {
      kind: "p",
      text: "Ringlite figures are the recommended package for that many people and numbers, before usage and taxes. Competitor figures use each vendor's published monthly seat price, include the numbers their seats carry, and add extra numbers only where the vendor publishes a price for them.",
    },
    { kind: "h2", text: "Ringlite's packages" },
    {
      kind: "list",
      items: PLANS.filter(plan => plan.price !== null).map(
        plan =>
          `${plan.name}: ${priceLabel(plan.price)} a month${
            plan.included
              ? ` for ${plan.included.users} ${plan.included.users === 1 ? "user" : "users"} and ${plan.included.numbers} ${
                  plan.included.numbers === 1 ? "number" : "numbers"
                }`
              : ""
          }.`,
      ),
    },
    {
      kind: "p",
      text: `Extra users and extra numbers are monthly add-ons on the paid plans, and paying yearly bills ${YEARLY.monthsBilled} months instead of twelve (${YEARLY.label}).`,
    },
    { kind: "h2", text: "What the headline price leaves out" },
    { kind: "p", text: "The number on a pricing page is rarely the whole bill. These are the lines to look for." },
    {
      kind: "list",
      items: [
        `Call usage past the included pool. Ringlite bills ${cents(RATES.minute)} a minute after the pool, and a bundle of 1,000 minutes costs ${money(RATES.minuteBundle.price)}.`,
        `Texts. Ringlite bills ${cents(RATES.text)} per segment, ${cents(RATES.picture)} for a picture message and ${cents(RATES.faxPage)} a page for fax, with a 1,000-text bundle at ${money(RATES.textBundle.price)}.`,
        "Extra numbers. One number per seat runs out as soon as you want a line for the front desk and another for the field, and extra numbers are billed every month.",
        "Texting registration. 10DLC registration carries a one-time charge plus a small monthly carrier fee, shown as one total at checkout.",
        "Taxes and fees, which vary by where you are and are added on top of the price you see.",
        "\"Unlimited\" calling. Where a plan advertises unlimited minutes, that promise sits under a fair use policy, and the real limits are in the policy rather than on the pricing page.",
      ],
    },
    { kind: "h2", text: "Where Ringlite is not the cheapest" },
    {
      kind: "p",
      text: `Two honest notes before you decide. First, KrispCall Starter costs the same as Ringlite or less for a team of five or fewer, so if that is your size and the lowest monthly number is what matters, compare it directly. Second, coverage. Ringlite does ${COVERAGE} and nothing else, so if you call or text Canada, Alaska, Hawaii or international numbers, it will not do the job and a provider with wider coverage will.`,
    },
    {
      kind: "p",
      text: `Each Ringlite number carries up to ${CALLS_PER_NUMBER} live calls at once and about ${DAILY_OUTBOUND_PER_NUMBER} outbound calls a day. Heavy outbound teams give each caller their own number rather than sharing one line, and that is a cost to plan for.`,
    },
    {
      kind: "cta",
      text: "Put in your team size and number count and see the monthly total, before usage and taxes.",
      label: "Open the calculator",
      to: "/calculator",
    },
  ],
};

const QUO_VS_RINGLITE_COST: Post = {
  slug: "quo-vs-ringlite-cost",
  title: "Quo (OpenPhone) vs Ringlite: what a 5-person team pays",
  description:
    "A line-by-line cost comparison for a five-person team on Quo and Ringlite, including yearly pricing, what Quo does better, and how to switch.",
  published: "2026-10-03",
  topic: "Switching",
  readMinutes: 7,
  draft: false,
  blocks: [
    {
      kind: "p",
      text: "Quo, formerly OpenPhone, and Ringlite both sell business phone service by the month, and both include numbers with the plan. The plans are shaped differently, though, and the totals pull apart as a team grows. Here is what a five-person team pays on each, with three- and ten-person teams for context, and an honest note on what Quo does better.",
    },
    { kind: "h2", text: "Five people, five numbers" },
    {
      kind: "p",
      text: `Monthly, Ringlite's recommended package for five people and five numbers is ${money(
        ringliteMonthly(5),
      )}. Quo Business lists at ${money(competitorCost(QUO, 5, 5, "month"))} a month for the same five people, at one number per seat.`,
    },
    {
      kind: "p",
      text: `Paid yearly, Ringlite works out to ${money(
        monthlyWhenYearly(ringliteMonthly(5)),
      )} a month, because yearly billing charges ${YEARLY.monthsBilled} months for the year (${YEARLY.label}). Quo Business at its yearly rate comes to ${money(
        competitorCost(QUO, 5, 5, "year"),
      )} a month.`,
    },
    { kind: "h2", text: "Three people and ten people" },
    QUO_TABLE,
    {
      kind: "p",
      text: "Ringlite is lower at every size shown, and the gap widens as the team grows, because Quo charges a seat price for every person while Ringlite's bundle absorbs the first few and then bills only the extras.",
    },
    { kind: "h2", text: "What Quo does better" },
    {
      kind: "p",
      text: "Quo is a good product, and it does some things Ringlite does not. It has more integrations, mobile apps, and thousands of public reviews, which matters if you want a vendor with a long track record and a large app directory behind it. If those are the deciding factors for your team, that is a fair reason to choose Quo, and it is worth saying plainly rather than pretending the choice is one-sided.",
    },
    { kind: "h2", text: "How usage is billed" },
    {
      kind: "p",
      text: `Ringlite's Team plan, at ${priceLabel(
        planByCode("team").price,
      )} a month, covers three people and three numbers, and Team and Business include a shared pool of call minutes. Calls past the pool are billed at ${cents(
        RATES.minute,
      )} a minute, so a busy month is a known number rather than a surprise.`,
    },
    {
      kind: "p",
      text: "Some plans elsewhere advertise unlimited calling, which lives under a fair use policy: the limits are written in the policy, not on the pricing page. Neither model is dishonest. They are different promises. With Ringlite you can estimate a heavy month in advance; with a fair use policy you find the line when you cross it.",
    },
    { kind: "h2", text: "How to switch" },
    {
      kind: "p",
      text: "If you decide to move, the order matters. Start on Ringlite with new numbers while Quo keeps running, then port your existing numbers. Porting is included on Team and Business and usually takes one to two weeks, and your old service keeps working until the transfer completes, so calls keep being answered the whole time.",
    },
    {
      kind: "p",
      text: "Start with [the Quo alternatives page](/alternatives/quo) and [the full Quo comparison](/compare/quo), then follow [the switching steps](/switch) when you are ready to move your numbers. If it helps to see the total first, [the calculator](/calculator) will price your team in a few seconds.",
    },
    {
      kind: "cta",
      text: "Price your own team and numbers, and compare the monthly and yearly totals.",
      label: "Open the calculator",
      to: "/calculator",
    },
  ],
};

/** The competitors that get their own side-by-side cost guide. */
const COMPARE_SLUGS = ["ringcentral", "aircall", "krispcall", "callhippo"] as const;

/** "Quo (OpenPhone)" reads as "Quo" in running copy. */
function shortName(name: string): string {
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

/** "3", "3 and 5", "3, 5 and 10". */
function joinWords(parts: readonly (string | number)[]): string {
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/**
 * A cost guide for one competitor: the same team priced on both, the honest split of which is
 * cheaper at which size, and how a switch works. Every number and name is read from the copy
 * and pricing sources, so nothing here can drift out of date on its own.
 */
function comparisonPost(slug: (typeof COMPARE_SLUGS)[number]): Post {
  const c = COMPETITORS.find(item => item.slug === slug);
  if (!c) throw new Error(`No competitor copy for ${slug}`);
  const p = COMPETITOR_SEAT_PRICES.find(item => item.slug === slug);
  if (!p) throw new Error(`No competitor price for ${slug}`);

  const short = shortName(c.name);
  const themMonthly = (users: number): number => Math.round(competitorCost(p, users, users));
  const themYearly = (users: number): number => Math.round(competitorCost(p, users, users, "year"));

  const cheaperForUs = SIZES.filter(n => ringliteMonthly(n) < themMonthly(n));
  const cheaperForThem = SIZES.filter(n => themMonthly(n) <= ringliteMonthly(n));
  const sentences: string[] = [];
  if (cheaperForUs.length > 0) {
    sentences.push(`Ringlite costs less at ${joinWords(cheaperForUs)} people.`);
  }
  if (cheaperForThem.length > 0) {
    sentences.push(`${short} costs the same or less at ${joinWords(cheaperForThem)} people.`);
  }

  const missesNumbers = SIZES.some(n => missesNumberPrice(p, n, n));
  const caption =
    `Monthly and yearly list prices before usage and taxes, checked ${COMPETITORS_CHECKED}.` +
    (missesNumbers
      ? ` Extra numbers are left out of the ${short} total because ${short} does not publish their price.`
      : "");

  const costTable: Block = {
    kind: "table",
    head: [
      "People and numbers",
      "Ringlite monthly",
      "Ringlite yearly, per month",
      `${short} monthly`,
      `${short} yearly, per month`,
    ],
    rows: SIZES.map(n => [
      String(n),
      `${recommend(n, n).plan.name} ${money(ringliteMonthly(n))}`,
      money(monthlyWhenYearly(ringliteMonthly(n))),
      `${competitorTier(p, n).name} ${money(themMonthly(n))}`,
      competitorTier(p, n).yearly !== null ? money(themYearly(n)) : "Not published",
    ]),
    caption,
  };

  const differTable: Block = {
    kind: "table",
    head: ["", short, "Ringlite"],
    rows: c.rows.map(row => [row.label, row.them, row.us]),
  };

  const links: string[] = [];
  if ((ALTERNATIVE_SLUGS as readonly string[]).includes(slug)) {
    links.push(`[the ${short} alternative page](/alternatives/${slug})`);
  }
  links.push(`[the full comparison](/compare/${slug})`);
  links.push("[how switching works](/switch)");

  return {
    slug: `${slug}-vs-ringlite-cost`,
    title: `${c.name} vs Ringlite: what a small team pays`,
    description: `${short} vs Ringlite for teams of 3 to 15: monthly and yearly list prices side by side, where each one is cheaper, and how switching works.`,
    published: "2026-10-03",
    topic: "Switching",
    readMinutes: 6,
    draft: false,
    blocks: [
      {
        kind: "p",
        text: `${c.summary} This guide prices the same team on both, with list prices checked ${COMPETITORS_CHECKED}.`,
      },
      { kind: "h2", text: "What the same team pays" },
      costTable,
      { kind: "p", text: sentences.join(" ") },
      { kind: "h2", text: "How the plans differ" },
      differTable,
      { kind: "h2", text: `Where ${short} is the better pick` },
      {
        kind: "list",
        items: [
          ...c.theyWin,
          `Coverage: Ringlite covers ${COVERAGE} only, so teams calling Canada or other countries need a provider with wider coverage.`,
        ],
      },
      { kind: "h2", text: "Where Ringlite is the better pick" },
      { kind: "list", items: c.weWin },
      { kind: "h2", text: "How usage is billed" },
      {
        kind: "p",
        text: `Team and Business include a shared pool of call minutes (${minutePoolsLine()} a month). Starter is pay as you go. Calls past the pool cost ${cents(RATES.minute)} a minute, and texts cost ${cents(RATES.text)} per text. "Unlimited" plans elsewhere sit under fair use policies.`,
      },
      { kind: "h2", text: `Switching from ${short}` },
      {
        kind: "p",
        text: `You can start on new Ringlite numbers the day you are approved, keep ${short} running while you do, and port your existing numbers on Team and Business, which usually takes one to two weeks.`,
      },
      { kind: "p", text: `Start with ${joinWords(links)}.` },
      {
        kind: "cta",
        text: "Price your own team and numbers, monthly or yearly.",
        label: "Open the calculator",
        to: "/calculator",
      },
    ],
  };
}

/** Every guide, newest first. Order ties on `published` by the order they appear here. */
export const POSTS: Post[] = [
  WHAT_IS_10DLC,
  BUSINESS_TEXTS_NOT_DELIVERED,
  BUSINESS_PHONE_SYSTEM_COST,
  QUO_VS_RINGLITE_COST,
  ...COMPARE_SLUGS.map(comparisonPost),
];

/** Guides safe to show in a production build. */
export const PUBLISHED_POSTS: Post[] = POSTS.filter(post => !post.draft);

/** Production builds (the live site and the prerender) hide drafts; dev and tests show them for review. */
export const IS_PROD_BUILD = (import.meta as unknown as { env?: { PROD?: boolean } }).env?.PROD === true;

export function postBySlug(slug: string): Post | undefined {
  return POSTS.find(post => post.slug === slug);
}
