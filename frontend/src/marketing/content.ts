/**
 * Copy for the product, solution and compare pages. Page components are templates; every
 * word a visitor reads on those pages comes from here (prices come from pricing.config).
 * Only list what Ringlite actually ships. Never claim SOC 2, HIPAA or review scores.
 */
import type { PlanCode } from "./pricing.config";

export type IconName =
  | "phone" | "message" | "inbox" | "bot" | "list" | "printer" | "plug" | "hash"
  | "home" | "key" | "wrench" | "scale" | "shield" | "users" | "briefcase" | "building";

export interface Product {
  slug: string;
  icon: IconName;
  menu: string;
  menuHint: string;
  eyebrow: string;
  title: string;
  lede: string;
  points: { title: string; body: string }[];
  steps: string[];
  plan: PlanCode;
  faqIds: string[];
}

export const PRODUCTS: Product[] = [
  {
    slug: "numbers", icon: "hash", menu: "Business numbers", menuHint: "Local, toll-free, bring your own",
    eyebrow: "BUSINESS NUMBERS", title: "A number your customers remember.",
    lede: "Pick a local number in your area code, add toll-free, or bring the number you already have. Every number carries E911 and belongs to a verified business.",
    points: [
      { title: "Local in your market", body: "Search by area code and choose the numbers you want after approval." },
      { title: "Keep your number", body: "Port in from your current carrier. You stay live on the old one until the move completes." },
      { title: "One line, many people", body: "Share a number across your team, or give heavy callers their own." },
    ],
    steps: ["Get approved", "Search by area code", "Pick your numbers and pay"],
    plan: "starter", faqIds: ["local-numbers", "toll-free", "port-in", "number-per-person"],
  },
  {
    slug: "calling", icon: "phone", menu: "Calling", menuHint: "Browser calls, menus, queues, voicemail",
    eyebrow: "CALLING", title: "Answer from anywhere with a browser.",
    lede: "Make and take calls from your computer. Ring the whole team or route through a menu, and send whatever you miss to voicemail in the same inbox.",
    points: [
      { title: "Nothing to install", body: "Calls run in Chrome, Edge, Firefox or Safari with any headset." },
      { title: "Menus and queues", body: "Press 1 for sales, business hours, and queues so the next free person answers." },
      { title: "Recording and voicemail", body: "Every call and voicemail lands in the customer's conversation." },
    ],
    steps: ["Choose a number", "Invite your team", "Set your menu and hours"],
    plan: "team", faqIds: ["calls-at-once", "ivr", "recording", "voicemail", "coverage"],
  },
  {
    slug: "texting", icon: "message", menu: "Business texting", menuHint: "SMS, MMS, 10DLC done inside",
    eyebrow: "BUSINESS TEXTING", title: "Texts that actually get delivered.",
    lede: "Carriers filter texts from businesses that are not registered. Ringlite has the 10DLC brand and campaign forms built in, and tracks approval for you.",
    points: [
      { title: "Registration built in", body: "Fill in your brand and campaign once. We submit it and show the status." },
      { title: "Pictures too", body: "Send photos and documents as picture messages." },
      { title: "Opt-outs handled", body: "STOP and other opt-out keywords are respected automatically." },
    ],
    steps: ["Register your brand", "Register your campaign", "Start texting after approval"],
    plan: "starter", faqIds: ["10dlc", "10dlc-time", "10dlc-cost", "consent", "mms"],
  },
  {
    slug: "inbox", icon: "inbox", menu: "Shared inbox", menuHint: "Calls and texts in one thread",
    eyebrow: "SHARED INBOX", title: "The whole story, in one place.",
    lede: "Every call, voicemail and text with a customer sits in one conversation. Your team sees the history before they pick up.",
    points: [
      { title: "One thread per customer", body: "Calls, texts and voicemail in order, across every number." },
      { title: "Notes for the team", body: "@mention a teammate. Notes are never sent to the customer." },
      { title: "Clean handoffs", body: "Assign a conversation and the next person starts with context." },
    ],
    steps: ["Invite your team", "Share your lines", "Work from one inbox"],
    plan: "starter", faqIds: ["shared-inbox", "notes", "users"],
  },
  {
    slug: "ai", icon: "bot", menu: "AI agents", menuHint: "Answer calls, draft replies",
    eyebrow: "AI AGENTS", title: "Help when the team is busy.",
    lede: "An AI voice agent answers calls and takes messages, and AI drafts replies to incoming texts for your team to send.",
    points: [
      { title: "AI voice agent", body: "Answers overflow and after-hours calls and records what the caller needs." },
      { title: "AI text replies", body: "Drafts a reply your team can send or edit." },
      { title: "You stay in charge", body: "Every AI conversation lands in the inbox for your team to review." },
    ],
    steps: ["Describe your business", "Choose when the agent answers", "Review conversations in the inbox"],
    plan: "business", faqIds: ["ai-agent", "ai-text"],
  },
  {
    slug: "dialer", icon: "list", menu: "Power dialer", menuHint: "Work a call list, one call at a time",
    eyebrow: "POWER DIALER", title: "Get through the list, not the busywork.",
    lede: "Load a list and call it one number after another, with notes and outcomes saved to each contact. Built for people talking to people, never robocalls.",
    points: [
      { title: "One click to the next call", body: "No dialing by hand, no copying numbers." },
      { title: "Outcomes saved", body: "Log what happened and move on." },
      { title: "Numbers stay healthy", body: "Calls spread across your lines so no number gets flagged." },
    ],
    steps: ["Import a list", "Start dialing", "Review outcomes"],
    plan: "business", faqIds: ["dialer", "spam-likely", "calls-at-once"],
  },
  {
    slug: "fax", icon: "printer", menu: "Fax", menuHint: "Send and receive in the browser",
    eyebrow: "FAX", title: "Fax without a fax machine.",
    lede: "Send and receive faxes from the same numbers and the same inbox.",
    points: [
      { title: "Send a PDF", body: "Upload and send, and get a delivery report." },
      { title: "Receive in the inbox", body: "Incoming faxes arrive as PDFs." },
      { title: "Pay per page", body: "No monthly fax line." },
    ],
    steps: ["Choose a number", "Upload a PDF", "Send"],
    plan: "business", faqIds: ["fax"],
  },
  {
    slug: "integrations", icon: "plug", menu: "Integrations and API", menuHint: "Webhooks, API keys",
    eyebrow: "INTEGRATIONS AND API", title: "Connect Ringlite to the tools you use.",
    lede: "Send call and text events to your CRM with webhooks, or build on the API.",
    points: [
      { title: "Webhooks", body: "Get an event for every call, text and voicemail." },
      { title: "API keys", body: "Scoped keys you can restrict by network." },
      { title: "Reports", body: "See calls and texts by number and by person." },
    ],
    steps: ["Create an API key", "Add a webhook", "Connect your CRM"],
    plan: "team", faqIds: ["plans"],
  },
];

export interface Solution {
  slug: string;
  icon: IconName;
  menu: string;
  menuHint: string;
  title: string;
  lede: string;
  thread: { contact: string; context: string; missed: string; incoming: string; outgoing: string; note: string };
  pains: { label: string; title: string; body: string }[];
  plan: PlanCode;
  faqIds: string[];
}

export const SOLUTIONS: Solution[] = [
  {
    slug: "real-estate", icon: "home", menu: "Real estate", menuHint: "Investors, agents, wholesalers",
    title: "Every seller. Every call. One thread.",
    lede: "Investors, agents and wholesalers run on callbacks. Ringlite keeps the missed call, the follow-up text and your partner's note on the same seller.",
    thread: { contact: "Dana Mitchell", context: "Seller · 4118 Elm St", missed: "Missed call · 9:12 AM · voicemail 0:41", incoming: "Is your offer on the Elm St house still open? My sister wants to sell too.", outgoing: "It is. I can walk both properties Thursday. Does 10am work?", note: "@Sam Two properties now. Run comps on the sister's before Thursday." },
    pains: [
      { label: "THE MISSED CALL", title: "Sellers call once.", body: "Ring the whole team at once, catch the rest with voicemail and text back from the same number." },
      { label: "THE HANDOFF", title: "Deals change hands.", body: "Acquisitions to dispositions, agent to coordinator. Notes and history travel with the contact." },
      { label: "THE TEXT", title: "Texts that land.", body: "Register once inside Ringlite so your follow-ups are not filtered." },
    ],
    plan: "team", faqIds: ["dialer", "10dlc", "consent", "number-per-person"],
  },
  {
    slug: "property-management", icon: "key", menu: "Property management", menuHint: "One line for tenants",
    title: "One number for every tenant.",
    lede: "Give tenants one number to call or text, and let whoever is on duty answer. Maintenance requests, photos and notes stay in one conversation.",
    thread: { contact: "Unit 4B · Priya", context: "Tenant · Maple Court", missed: "Missed call · 7:48 PM", incoming: "The kitchen sink is leaking under the cabinet. Photo attached.", outgoing: "Thanks Priya. A plumber can come tomorrow between 9 and 11.", note: "@Luis Work order opened. Tenant home all morning." },
    pains: [
      { label: "AFTER HOURS", title: "Leaks don't wait.", body: "Route after-hours calls to whoever is on call, with voicemail as the backstop." },
      { label: "PHOTOS", title: "See the problem first.", body: "Tenants text photos straight into the conversation." },
      { label: "HANDOFF", title: "Office to field.", body: "Notes tell the technician exactly what was promised." },
    ],
    plan: "team", faqIds: ["ivr", "mms", "shared-inbox"],
  },
  {
    slug: "home-services", icon: "wrench", menu: "Home services", menuHint: "Never miss the 7pm job",
    title: "Answer the job that calls at 7pm.",
    lede: "Plumbers, HVAC, roofers and cleaners win the job by answering first. Ring the whole crew, text quotes and keep every customer in one thread.",
    thread: { contact: "Mark Owens", context: "New lead · HVAC", missed: "Missed call · 7:02 PM", incoming: "AC stopped blowing cold. Can someone come tonight?", outgoing: "Yes, a tech can be there by 8:30. Sending the call-out fee now.", note: "@Jen Booked for 8:30. Gate code is 4410." },
    pains: [
      { label: "SPEED", title: "First to answer wins.", body: "Ring everyone at once so no lead goes to voicemail." },
      { label: "QUOTES", title: "Text the quote.", body: "Send prices and photos by text and keep the reply." },
      { label: "CREW", title: "The tech knows the story.", body: "Notes and history travel to whoever takes the job." },
    ],
    plan: "team", faqIds: ["forwarding", "voicemail", "texting-how"],
  },
  {
    slug: "law-firms", icon: "scale", menu: "Law firms", menuHint: "Intake, recordings and notes",
    title: "Intake that never slips.",
    lede: "Every intake call, voicemail and follow-up text in one place, with notes for the attorney and a clean record of who said what.",
    thread: { contact: "R. Alvarez", context: "Intake · Consultation", missed: "Voicemail · 2:14 PM · 1:12", incoming: "I received the documents. Do I need to bring originals Monday?", outgoing: "Yes, please bring the originals and a photo ID.", note: "@Attorney Intake complete. Conflict check clear." },
    pains: [
      { label: "INTAKE", title: "Every caller is a case.", body: "Menus and queues put new callers in front of intake." },
      { label: "RECORD", title: "Know what was said.", body: "Recordings and notes sit with the client's conversation." },
      { label: "FOLLOW-UP", title: "Reminders by text.", body: "Confirm appointments and documents from the firm's number." },
    ],
    plan: "team", faqIds: ["recording", "ivr", "roles"],
  },
  {
    slug: "insurance", icon: "shield", menu: "Insurance agencies", menuHint: "Quote follow-ups that don't slip",
    title: "Every quote gets a follow-up.",
    lede: "Keep quotes, renewals and claims calls in one shared history so any agent can pick up where the last one stopped.",
    thread: { contact: "Tom Becker", context: "Auto quote · Renewal", missed: "Missed call · 11:30 AM", incoming: "Did you find a better rate on the renewal?", outgoing: "I did. It's $38 less a month. Can I call you at 2?", note: "@Ana Send the comparison sheet before the call." },
    pains: [
      { label: "RENEWALS", title: "Timing is everything.", body: "Call and text from the agency's number so clients recognise you." },
      { label: "TEAM", title: "Any agent can help.", body: "The full history is in the conversation." },
      { label: "VOLUME", title: "Work the list.", body: "The power dialer moves through renewals one call at a time." },
    ],
    plan: "business", faqIds: ["dialer", "shared-inbox", "consent"],
  },
  {
    slug: "recruiting", icon: "briefcase", menu: "Recruiting", menuHint: "Candidates reply by text",
    title: "Candidates answer texts.",
    lede: "Reach candidates by text, call when they are ready, and keep every touchpoint on one thread your whole desk can see.",
    thread: { contact: "Jordan Lee", context: "Candidate · Warehouse lead", missed: "Missed call · 5:40 PM", incoming: "Got your message. I'm free to talk after 6.", outgoing: "Great, I'll call at 6:15 about the Thursday interview.", note: "@Kim Candidate confirmed. Prefers texts." },
    pains: [
      { label: "REPLIES", title: "Texts get answered.", body: "Reach candidates where they respond." },
      { label: "DESK", title: "Shared pipeline.", body: "Any recruiter can see the latest conversation." },
      { label: "PACE", title: "More calls, less dialing.", body: "Work candidate lists with the power dialer." },
    ],
    plan: "team", faqIds: ["texting-how", "consent", "dialer"],
  },
];

export const TEAM_SOLUTIONS: { slug: string; icon: IconName; menu: string; menuHint: string }[] = [
  { slug: "solo", icon: "phone", menu: "Solo operators", menuHint: "A real business line" },
  { slug: "small-teams", icon: "users", menu: "Small teams", menuHint: "3 users and 3 numbers included" },
  { slug: "sales", icon: "list", menu: "Sales teams", menuHint: "Dialer, texting, follow-ups" },
];

export interface Competitor {
  slug: string;
  name: string;
  summary: string;
  rows: { label: string; them: string; us: string }[];
  theyWin: string[];
}

export const COMPETITORS: Competitor[] = [
  {
    slug: "quo", name: "Quo (OpenPhone)",
    summary: "Quo is a polished per-user phone system. Ringlite packages users and numbers in one monthly price.",
    rows: [
      { label: "How you pay", them: "Per user", us: "One monthly package with users and numbers" },
      { label: "Calling", them: "\"Unlimited\" under a discretionary fair use policy", us: "An exact shared pool (Team 200, Business 1,000 min), then a published per-minute rate" },
      { label: "Texting registration", them: "Yes", us: "Built in" },
      { label: "Free trial", them: "7 days", us: "Welcome credit after approval" },
    ],
    theyWin: ["More integrations today", "Mobile apps", "Thousands of public reviews"],
  },
  {
    slug: "ringcentral", name: "RingCentral",
    summary: "RingCentral is built for large companies. Ringlite is self-serve and priced for small teams.",
    rows: [
      { label: "How you pay", them: "Per user, prices through sales", us: "Packages, prices on the page" },
      { label: "Texting", them: "25 to 200 texts per user", us: "Texts at a published rate, no monthly cap" },
      { label: "Setup", them: "Sales-led", us: "Sign up and get approved in about an hour" },
    ],
    theyWin: ["Video meetings and contact center", "International calling", "Desk phones"],
  },
  {
    slug: "callhippo", name: "CallHippo",
    summary: "CallHippo sells many add-ons per user. Ringlite puts the essentials in three plans.",
    rows: [
      { label: "How you pay", them: "Per user plus add-ons", us: "One monthly package with users and numbers" },
      { label: "Power dialer", them: "Paid add-on", us: "Included on Business" },
      { label: "Rates", them: "Bundled", us: "Published per minute and per text" },
    ],
    theyWin: ["International numbers", "WhatsApp"],
  },
  {
    slug: "krispcall", name: "KrispCall",
    summary: "KrispCall charges per user and meters usage separately. Ringlite includes users and numbers in each plan and publishes every rate.",
    rows: [
      { label: "How you pay", them: "Per user, usage extra", us: "Package price, usage at published rates" },
      { label: "Team size", them: "Entry plan up to 5 users", us: "Users included on every plan" },
    ],
    theyWin: ["Numbers in 100+ countries"],
  },
  {
    slug: "aircall", name: "Aircall",
    summary: "Aircall needs at least three paid licenses. Ringlite starts at one user and one number.",
    rows: [
      { label: "Minimum", them: "3 licenses", us: "1 user and 1 number" },
      { label: "How you pay", them: "Per license", us: "One monthly package with users and numbers" },
      { label: "Texting", them: "250 texts per user", us: "Texts at a published rate, no monthly cap" },
    ],
    theyWin: ["250+ integrations", "Call-center analytics"],
  },
];

export const PROOF_POINTS = [
  "Every account identity-verified",
  "Carrier-registered texting",
  "E911 on every number",
  "Rates published to the minute",
];
