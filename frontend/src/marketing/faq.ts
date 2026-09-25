/**
 * Pre-answered questions. Shown on /faq, on the pricing page and on product pages, and used
 * by the chat assistant, which answers from this list first and only falls back to the
 * backend (which is also limited to this list) when nothing here matches.
 *
 * Answers that quote prices are functions of the pricing config so they never drift.
 * `keywords` are lower-case words or phrases the chat matcher scores against.
 */
import { CALLS_PER_NUMBER, COVERAGE, DAILY_OUTBOUND_PER_NUMBER, PRICED_PLANS, RATES, cents, money, planByCode } from "./pricing.config";

export type FaqTopic =
  | "Getting started"
  | "Plans and billing"
  | "Numbers and porting"
  | "Calling"
  | "Texting and 10DLC"
  | "Team and users"
  | "AI features"
  | "Security and 911"
  | "Switching to Ringlite";

export interface Faq {
  id: string;
  topic: FaqTopic;
  q: string;
  a: string;
  keywords: string[];
  /** A question the assistant should hand to a person rather than answer alone. */
  handoff?: boolean;
}

const starter = planByCode("starter");
const team = planByCode("team");
const business = planByCode("business");

const planLine = PRICED_PLANS.map(p => {
  const inc = p.included ? `${p.included.users} ${p.included.users === 1 ? "user" : "users"} and ${p.included.numbers} ${p.included.numbers === 1 ? "number" : "numbers"}` : "";
  return `${p.name} ${money(p.price ?? 0)} a month with ${inc}`;
}).join(", ");

export const FAQS: Faq[] = [
  // Getting started
  { id: "what-is", topic: "Getting started", q: "What is Ringlite?", a: "Ringlite gives your business its own phone numbers, calling from the browser, business texting and one shared inbox where your whole team sees every call and text with a customer.", keywords: ["what is", "ringlite", "what do you do", "about"] },
  { id: "signup-steps", topic: "Getting started", q: "How do I sign up?", a: "Sign up with a personal or work email, confirm it and secure your account. Then verify your identity with Didit and submit your application. Once approved, you pick your numbers by area code, pay, and your inbox is ready.", keywords: ["sign up", "signup", "get started", "register", "create account", "open account"] },
  { id: "review-time", topic: "Getting started", q: "How long does approval take?", a: "Most reviews finish in under an hour. Some applications need a closer look and take longer; we email you either way.", keywords: ["how long", "approval", "approved", "review", "waiting", "pending"] },
  { id: "why-verify", topic: "Getting started", q: "Why do you verify my identity?", a: "Every Ringlite account belongs to a verified person. It keeps our numbers off spam lists, protects carriers and your customers, and is why your calls and texts get delivered.", keywords: ["why verify", "identity", "didit", "id check", "kyc", "verification"] },
  { id: "personal-email", topic: "Getting started", q: "Can I sign up with a personal email?", a: "Yes. Personal and work emails both work. Everyone goes through the same identity check; you do not need a registered company to start calling.", keywords: ["personal email", "gmail", "no company", "sole proprietor", "individual"] },
  { id: "id-declined", topic: "Getting started", q: "My identity check was declined. What now?", a: "A person on our team should look at this with you. We will review what happened and tell you the next step.", keywords: ["declined", "rejected", "failed verification", "not approved"], handoff: true },
  { id: "devices", topic: "Getting started", q: "What do I need to make calls?", a: "A computer with a current browser (Chrome, Edge, Firefox or Safari) and a headset or built-in microphone. There is nothing to install.", keywords: ["browser", "install", "headset", "device", "computer", "what do i need"] },
  { id: "trial", topic: "Getting started", q: "Is there a free trial?", a: "There is no free trial. Verified workspaces get a small welcome credit to test calling, and plans are month to month, so you can cancel any time.", keywords: ["free trial", "trial", "try free", "test it", "demo account"] },

  // Plans and billing
  { id: "plans", topic: "Plans and billing", q: "What plans do you offer?", a: `${planLine}. Need more? Extra users and numbers can be added to any plan, and bigger teams can talk to us about a Custom plan. Calls and texts are pay as you go on every plan.`, keywords: ["plans", "pricing", "price", "how much", "cost", "tiers", "packages"] },
  { id: "per-number", topic: "Plans and billing", q: "How does pricing work?", a: `Each plan is one monthly price that includes a set number of users and phone numbers. Add users or numbers whenever you need them: on Starter and Team an extra user is ${money(starter.extraUser ?? 0)} and an extra number ${money(starter.extraNumber ?? 0)}; on Business an extra user is ${money(business.extraUser ?? 0)} and an extra number ${money(business.extraNumber ?? 0)}. Calls and texts are pay as you go.`, keywords: ["per number", "per user", "per seat", "per line", "how does pricing", "add user", "add number"] },
  { id: "what-counts", topic: "Plans and billing", q: "What does each plan include?", a: `Starter includes 1 user and 1 number, Team 3 users and 3 numbers, Business 10 users and 10 numbers, plus the features listed on the pricing page. Calls (${cents(RATES.minute)} a minute) and texts (${cents(RATES.text)}) are paid as you use them.`, keywords: ["include", "included", "what do i get", "allowance"] },
  { id: "overage", topic: "Plans and billing", q: "Are minutes included?", a: `Calls and texts are pay as you go on every plan: ${cents(RATES.minute)} a minute and ${cents(RATES.text)} a text, from a prepaid balance. Heavy users can buy bundles, like ${RATES.minuteBundle.units.toLocaleString()} minutes for ${money(RATES.minuteBundle.price)}.`, keywords: ["minutes included", "free minutes", "overage", "run out", "more minutes", "extra minutes", "limit", "pool"] },
  { id: "unlimited", topic: "Plans and billing", q: "Is calling unlimited?", a: `No, and we say so on purpose. "Unlimited" plans elsewhere hide a fair use cap in their legal pages. With Ringlite you pay ${cents(RATES.minute)} a minute for what you use, so light callers pay far less.`, keywords: ["unlimited", "fair use", "fup", "cap"] },
  { id: "rates", topic: "Plans and billing", q: "What are your usage rates?", a: `Calls ${cents(RATES.minute)} a minute, texts ${cents(RATES.text)} a segment, picture messages ${cents(RATES.picture)}, fax ${cents(RATES.faxPage)} a page. Bundles: ${RATES.minuteBundle.units.toLocaleString()} minutes for ${money(RATES.minuteBundle.price)}, ${RATES.textBundle.units.toLocaleString()} texts for ${money(RATES.textBundle.price)}.`, keywords: ["rate", "rates", "per minute", "per text", "cents", "rate card", "mms", "fax price"] },
  { id: "prepaid", topic: "Plans and billing", q: "How is usage paid for?", a: "Calls and texts are taken from a prepaid balance that you top up by card, with optional auto-recharge. You always see your balance before you spend it.", keywords: ["prepaid", "balance", "top up", "credit", "auto recharge", "pay usage"] },
  { id: "spend-limits", topic: "Plans and billing", q: "Why is there a daily spending limit?", a: "New workspaces have a low daily usage limit that rises after the first month. It protects you if a password or card is ever stolen. Contact us if you need it raised sooner.", keywords: ["daily limit", "spending limit", "spend limit", "blocked spending"] },
  { id: "cancel", topic: "Plans and billing", q: "Can I cancel any time?", a: "Yes. Monthly plans end at the close of the billing month. You can release numbers from Settings; porting a number out to another provider is also possible.", keywords: ["cancel", "cancellation", "stop subscription", "contract", "commitment"] },
  { id: "taxes", topic: "Plans and billing", q: "Are taxes and fees included?", a: "Prices are before applicable taxes. Texting registration (10DLC) has a one-time charge shown as a single total at checkout. There are no hidden line-item fees.", keywords: ["tax", "taxes", "fees", "hidden fees", "surcharge"] },
  { id: "refund", topic: "Plans and billing", q: "Can I get a refund?", a: "A person on our team handles refunds so we can look at your account.", keywords: ["refund", "money back", "charged wrong", "billing problem", "invoice"], handoff: true },
  { id: "change-plan", topic: "Plans and billing", q: "Can I change plans later?", a: "Yes. Upgrades apply straight away. You can move to a smaller plan once your users and numbers fit it, or keep your plan and add users and numbers as you grow.", keywords: ["change plan", "upgrade", "downgrade", "switch plan"] },
  { id: "enterprise", topic: "Plans and billing", q: "Do you have a plan for bigger teams?", a: "Yes, the Custom plan: volume pricing on users, numbers and usage, help moving your numbers and a named contact. A person from our team will take it from here.", keywords: ["enterprise", "custom", "large team", "custom pricing", "volume", "100 users", "50 numbers", "many numbers"], handoff: true },

  // Numbers and porting
  { id: "local-numbers", topic: "Numbers and porting", q: "Can I choose a local number?", a: "Yes. Search available numbers by area code and pick the ones you want after approval.", keywords: ["local number", "area code", "choose number", "pick number", "local"] },
  { id: "toll-free", topic: "Numbers and porting", q: "Do you offer toll-free numbers?", a: "Yes. Toll-free numbers are available; texting from them uses a separate toll-free verification instead of 10DLC.", keywords: ["toll free", "toll-free", "800 number", "888"] },
  { id: "port-in", topic: "Numbers and porting", q: "Can I keep my existing number?", a: "Yes. Porting is included on Team and Business. Start a port from Numbers once your account is approved; you keep using your old provider until the move completes.", keywords: ["port", "porting", "keep my number", "transfer number", "bring my number", "move my number"] },
  { id: "port-time", topic: "Numbers and porting", q: "How long does porting take?", a: "Usually one to two weeks, depending on your current carrier. We show the status of each port in your account.", keywords: ["port time", "porting take", "how long port"] },
  { id: "more-numbers", topic: "Numbers and porting", q: "Can I add more numbers later?", a: `Yes, any time. Each extra number is ${money(team.extraNumber ?? 0)} a month on top of your plan.`, keywords: ["add number", "more numbers", "second number", "another number", "extra number"] },
  { id: "number-per-person", topic: "Numbers and porting", q: "Does every user need their own number?", a: "No. A number can be shared by your whole team. Teams that make a lot of outbound calls usually give each caller their own number, which keeps every number's reputation clean.", keywords: ["own number", "each user", "number per user", "share number", "shared number"] },

  // Calling
  { id: "coverage", topic: "Calling", q: "Where can I call?", a: `${COVERAGE}. International calling, Canada, Alaska, Hawaii and the US territories are not available, and premium-rate numbers are always blocked. 911 always works.`, keywords: ["where can i call", "international", "canada", "coverage", "countries", "alaska", "hawaii", "mexico", "uk"] },
  { id: "calls-at-once", topic: "Calling", q: "How many calls can we make at the same time?", a: `Each number carries up to ${CALLS_PER_NUMBER} live calls at once, and each person one. To have more people on calls together, add numbers.`, keywords: ["same time", "at once", "simultaneous", "concurrent", "lines busy", "busy"] },
  { id: "spam-likely", topic: "Calling", q: "Will my calls show as Spam Likely?", a: `We help prevent it: every account is verified, and a number carries at most ${CALLS_PER_NUMBER} calls at once and about ${DAILY_OUTBOUND_PER_NUMBER} outbound calls a day. Teams with heavy outbound give each caller their own number. We cannot promise how every carrier labels calls.`, keywords: ["spam", "spam likely", "scam likely", "flagged", "blocked calls", "reputation"] },
  { id: "voicemail", topic: "Calling", q: "Do I get voicemail?", a: "Yes, on every plan. Voicemails land in the inbox next to the caller's texts and calls.", keywords: ["voicemail", "voice mail", "missed call", "greeting"] },
  { id: "recording", topic: "Calling", q: "Can I record calls?", a: "Yes, on Team and Business. Check your state's consent rules; many require telling the other person the call is recorded.", keywords: ["record", "recording", "call recording"] },
  { id: "ivr", topic: "Calling", q: "Can I set up a phone menu?", a: "Yes. Team and Business include phone menus (press 1 for sales, 2 for support), call queues and business hours routing.", keywords: ["ivr", "phone menu", "menu", "press 1", "auto attendant", "queue", "business hours", "routing"] },
  { id: "forwarding", topic: "Calling", q: "Can calls ring more than one person?", a: "Yes. Calls can ring your whole team at once or go through a queue, so the next free person answers.", keywords: ["ring team", "ring group", "ring everyone", "forward", "forwarding", "transfer"] },
  { id: "dialer", topic: "Calling", q: "Do you have a power dialer?", a: "Yes, on Business. It dials your list one call at a time for each agent. Predictive dialers and robocalls are not allowed.", keywords: ["dialer", "power dialer", "auto dialer", "predictive", "cold call", "call list"] },
  { id: "fax", topic: "Calling", q: "Can I send and receive faxes?", a: `Yes, on Business, at ${cents(RATES.faxPage)} a page.`, keywords: ["fax", "efax"] },

  // Texting
  { id: "texting-how", topic: "Texting and 10DLC", q: "Can I text from my business number?", a: "Yes. For local US numbers you register your brand and a 10DLC campaign inside Ringlite. Texting switches on after carriers approve it.", keywords: ["text", "sms", "texting", "send text", "business texting"] },
  { id: "10dlc", topic: "Texting and 10DLC", q: "What is 10DLC?", a: "10DLC is the carrier registration every business needs to text from a local number in the US. Without it, carriers filter or block your messages. Ringlite has the forms built in.", keywords: ["10dlc", "10 dlc", "campaign registry", "tcr", "brand registration", "a2p"] },
  { id: "10dlc-time", topic: "Texting and 10DLC", q: "How long does texting approval take?", a: "Carriers usually approve within a few days to two weeks. We show the status in your account and tell you when texting is live.", keywords: ["texting approval", "10dlc time", "campaign approval", "how long texting"] },
  { id: "10dlc-cost", topic: "Texting and 10DLC", q: "What does texting registration cost?", a: "There is a one-time registration charge plus a small monthly carrier fee, shown as one total at checkout before you pay.", keywords: ["10dlc cost", "registration fee", "campaign fee", "texting cost"] },
  { id: "mms", topic: "Texting and 10DLC", q: "Can I send pictures?", a: `Yes, picture messages (MMS) are ${cents(RATES.picture)} each.`, keywords: ["mms", "picture", "photo", "image", "attachment"] },
  { id: "bulk-sms", topic: "Texting and 10DLC", q: "Can I send mass texts?", a: "You can text contacts who have agreed to hear from you. Unsolicited bulk texting is not allowed and carriers block it.", keywords: ["mass text", "bulk sms", "blast", "campaign text", "marketing text"] },
  { id: "consent", topic: "Texting and 10DLC", q: "Do I need consent to text customers?", a: "Yes. US law (the TCPA) and carrier rules require consent, and Ringlite handles opt-out keywords like STOP automatically.", keywords: ["consent", "opt in", "tcpa", "stop", "unsubscribe", "opt out"] },

  // Team
  { id: "users", topic: "Team and users", q: "How many people can use my account?", a: `Starter includes ${starter.included?.users}, Team ${team.included?.users} and Business ${business.included?.users}. Add more any time: ${money(team.extraUser ?? 0)} a user on Starter and Team, ${money(business.extraUser ?? 0)} on Business.`, keywords: ["how many users", "users", "seats", "teammates", "people", "staff", "employees"] },
  { id: "extra-user", topic: "Team and users", q: "Does adding a user add a phone number?", a: `No. Users and numbers are added separately, so several people can share one number. Each extra number is ${money(team.extraNumber ?? 0)} a month.`, keywords: ["extra user", "add user", "additional user", "user number"] },
  { id: "shared-inbox", topic: "Team and users", q: "How does the shared inbox work?", a: "Every call, voicemail and text with a customer sits in one conversation your team can see. Assign it, leave an internal note, and whoever picks it up next has the whole story.", keywords: ["inbox", "shared inbox", "conversation", "history", "thread"] },
  { id: "notes", topic: "Team and users", q: "Can my team leave notes?", a: "Yes. Internal notes and @mentions sit inside the conversation and are never sent to the customer.", keywords: ["notes", "internal note", "mention", "comment"] },
  { id: "roles", topic: "Team and users", q: "Can I control what each person can do?", a: "Yes. Business adds admin roles and an audit log of who did what.", keywords: ["roles", "permissions", "admin", "audit"] },

  // AI
  { id: "ai-agent", topic: "AI features", q: "What does the AI voice agent do?", a: "On Business, an AI agent can answer calls, take messages and handle simple questions when your team is busy. AI minutes are metered separately.", keywords: ["ai", "ai agent", "voice agent", "ai receptionist", "virtual receptionist", "bot answer"] },
  { id: "ai-text", topic: "AI features", q: "Can AI help reply to texts?", a: "Yes. On Business, AI drafts replies to incoming texts that your team can send or edit.", keywords: ["ai text", "ai reply", "auto reply", "suggested reply"] },

  // Security
  { id: "e911", topic: "Security and 911", q: "Does 911 work?", a: "Yes. Every number has E911. Keep your emergency address current in Settings, because 911 calls use it to send help. Browser calling depends on power and internet; read our 911 disclosure.", keywords: ["911", "e911", "emergency"] },
  { id: "security", topic: "Security and 911", q: "How do you keep my account safe?", a: "Every account is identity-verified, sign-in uses two-step verification, each workspace's data is kept separate, and fraud controls watch for unusual spending.", keywords: ["secure", "security", "safe", "2fa", "two factor", "privacy", "data"] },
  { id: "hipaa", topic: "Security and 911", q: "Are you HIPAA compliant?", a: "Not today. We cannot sign a business associate agreement yet, so Ringlite should not be used for protected health information.", keywords: ["hipaa", "baa", "health", "medical", "patient"] },
  { id: "report-abuse", topic: "Security and 911", q: "A Ringlite number called me and I don't know them.", a: "Report it at ringlite.io/report. We review every report.", keywords: ["report", "abuse", "harass", "unknown call", "spam call from"] },

  // Switching
  { id: "vs-quo", topic: "Switching to Ringlite", q: "How is Ringlite different from Quo (OpenPhone)?", a: "Quo charges per user. Ringlite includes users and numbers in one monthly package and publishes its per-minute rate instead of \"unlimited\". For small teams that usually costs much less.", keywords: ["quo", "openphone", "open phone"] },
  { id: "vs-ringcentral", topic: "Switching to Ringlite", q: "How is Ringlite different from RingCentral?", a: "RingCentral is built for large companies and sells through sales calls. Ringlite is self-serve, priced as simple packages with users and numbers included, and set up in under an hour.", keywords: ["ringcentral", "ring central"] },
  { id: "vs-callhippo", topic: "Switching to Ringlite", q: "How is Ringlite different from CallHippo or KrispCall?", a: "Both charge per user. Ringlite includes users and numbers in each plan for one monthly price and shows every rate up front.", keywords: ["callhippo", "call hippo", "krispcall", "krisp call"] },
  { id: "vs-aircall", topic: "Switching to Ringlite", q: "How is Ringlite different from Aircall?", a: "Aircall starts at three paid licenses. Ringlite starts at $15 a month for one user and one number.", keywords: ["aircall", "air call"] },
  { id: "migrate", topic: "Switching to Ringlite", q: "How do I switch from another provider?", a: "Sign up, get approved, then port your numbers in. Keep your old service until the port completes so you never miss a call.", keywords: ["switch", "migrate", "move from", "leave", "change provider"] },
  { id: "talk-to-person", topic: "Switching to Ringlite", q: "Can I talk to a person?", a: "Of course. A person from our team will join this chat.", keywords: ["human", "person", "agent", "talk to someone", "representative", "sales", "call me", "speak to"], handoff: true },
];

export const FAQ_TOPICS: FaqTopic[] = Array.from(new Set(FAQS.map(f => f.topic)));

export function faqsFor(topic: FaqTopic): Faq[] {
  return FAQS.filter(f => f.topic === topic);
}

export function faqById(id: string): Faq | undefined {
  return FAQS.find(f => f.id === id);
}

const normalise = (text: string) => ` ${text.toLowerCase().replace(/[^a-z0-9+ ]+/g, " ").replace(/\s+/g, " ").trim()} `;

/**
 * Facts sent with a question the FAQ matcher could not answer: the best-scoring entries plus
 * the plans and coverage answers, so the assistant can only answer from Ringlite's own copy.
 */
export function relevantFaqs(question: string, k = 8): { q: string; a: string }[] {
  const text = normalise(question);
  const scored = FAQS.map(faq => ({
    faq,
    score: faq.keywords.reduce((sum, keyword) => {
      const kw = normalise(keyword);
      return text.includes(kw) ? sum + kw.trim().split(" ").length + 1 : sum;
    }, 0),
  }))
    .filter(entry => entry.score > 0)
    .sort((x, y) => y.score - x.score)
    .map(entry => entry.faq);
  const always = ["plans", "per-number", "overage", "coverage"].map(faqById).filter((f): f is Faq => !!f);
  const picked: Faq[] = [];
  for (const faq of [...scored, ...always]) {
    if (!picked.includes(faq)) picked.push(faq);
    if (picked.length >= k) break;
  }
  return picked.map(faq => ({ q: faq.q, a: faq.a }));
}

/**
 * Best FAQ for a visitor's question, or null. A keyword counts once; longer phrases score
 * more because they are more specific. A score below `min` is no match.
 */
export function matchFaq(question: string, min = 2): Faq | null {
  const text = normalise(question);
  let best: Faq | null = null;
  let bestScore = 0;
  for (const faq of FAQS) {
    let score = 0;
    for (const keyword of faq.keywords) {
      const k = normalise(keyword);
      if (text.includes(k)) score += k.trim().split(" ").length + 1;
    }
    if (score > bestScore) {
      best = faq;
      bestScore = score;
    }
  }
  return bestScore >= min ? best : null;
}
