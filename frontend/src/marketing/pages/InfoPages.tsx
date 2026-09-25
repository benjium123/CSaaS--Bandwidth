/**
 * The four informational marketing pages: /sales, /faq, /security and /legal/911.
 * Every price and coverage claim comes from the shared marketing modules.
 */
import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowRightLeft,
  Clock,
  Flag,
  KeyRound,
  Lock,
  MessageSquare,
  PhoneCall,
  Plus,
  ShieldAlert,
  ShieldCheck,
  Users,
} from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { CtaBand, SitePage } from "@/marketing/SiteChrome";
import { FAQS, FAQ_TOPICS, type FaqTopic } from "@/marketing/faq";
import { COVERAGE, PLANS, money } from "@/marketing/pricing.config";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TEAM_SIZES = ["Just me", "2-3", "4-8", "9-25", "25+"];
const NUMBER_COUNTS = ["1", "2-4", "5-10", "11-50", "50+"];
const SWITCHING_FROM = [
  "Nothing yet",
  "Quo (OpenPhone)",
  "RingCentral",
  "CallHippo",
  "KrispCall",
  "Aircall",
  "Dialpad",
  "Other",
];

interface SalesErrors {
  name?: string;
  email?: string;
  teamSize?: string;
  numbersNeeded?: string;
}

type SendStatus = "idle" | "sending" | "failed" | "sent";

export function SalesPage() {
  const { api } = useAuth();
  const [params] = useSearchParams();
  const plan = params.get("plan");
  const industry = params.get("industry");

  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [phone, setPhone] = React.useState("");
  const [company, setCompany] = React.useState("");
  const [teamSize, setTeamSize] = React.useState("");
  const [numbersNeeded, setNumbersNeeded] = React.useState("");
  const [switchingFrom, setSwitchingFrom] = React.useState("Nothing yet");
  const [message, setMessage] = React.useState("");
  const [smsConsent, setSmsConsent] = React.useState(false);
  const [errors, setErrors] = React.useState<SalesErrors>({});
  const [status, setStatus] = React.useState<SendStatus>("idle");

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (status === "sending") return;

    const found: SalesErrors = {};
    if (!name.trim()) found.name = "Please tell us your name.";
    if (!email.trim()) found.email = "We need an email address to reply to.";
    else if (!EMAIL_PATTERN.test(email.trim())) found.email = "That email address doesn’t look right.";
    if (!teamSize) found.teamSize = "Choose how many people will use Ringlite.";
    if (!numbersNeeded) found.numbersNeeded = "Choose how many numbers you need.";

    setErrors(found);
    if (Object.keys(found).length > 0) {
      setStatus("idle");
      return;
    }

    setStatus("sending");
    try {
      await api.request<{ received: true }>("/api/v1/public/sales-leads", {
        method: "POST",
        json: {
          name: name.trim(),
          email: email.trim(),
          phone: phone.trim() || null,
          company: company.trim() || null,
          team_size: teamSize,
          numbers_needed: numbersNeeded,
          switching_from: switchingFrom,
          message: message.trim() || null,
          sms_consent: smsConsent,
          plan: plan || null,
          page: industry ? `/sales?industry=${industry}` : "/sales",
        },
      });
      setStatus("sent");
    } catch {
      setStatus("failed");
    }
  }

  return (
    <SitePage title="Talk to sales" description="Tell us about your team and we’ll find the right plan.">
      <section className="ms-sales rl-wrap">
        <div className="ms-sales-copy">
          <p className="rl-eyebrow"><span /> TALK TO SALES</p>
          <h1 className="ms-h1">Let’s find the right plan.</h1>
          <p className="ms-lede">Tell us how your team answers the phone and a person from Ringlite will match you to a plan. Moving from another provider? We will help with the numbers you already have.</p>
          <ul className="ms-reassure">
            <li><Clock size={18} aria-hidden="true" />A reply within one business day</li>
            <li><ArrowRightLeft size={18} aria-hidden="true" />Help moving your existing numbers</li>
            <li><Users size={18} aria-hidden="true" />Plans for 25+ people</li>
          </ul>
          <ul>
            {PLANS.map(item => (
              <li key={item.code}>
                <strong>{item.name}</strong> {money(item.pricePerNumber.yearly)}/number, billed yearly
              </li>
            ))}
          </ul>
          <p><Link className="rl-text-link" to="/pricing">See pricing</Link></p>
        </div>

        {status === "sent" ? (
          <div className="ms-success" role="status">
            <p>Thanks, {name.trim()}. We’ll be in touch within one business day.</p>
            <p><Link className="rl-text-link" to="/pricing">See pricing</Link></p>
          </div>
        ) : (
          <form className="ms-form" noValidate onSubmit={handleSubmit}>
            <div>
              <label htmlFor="sales-name">Your name</label>
              <input
                id="sales-name"
                name="name"
                type="text"
                autoComplete="name"
                value={name}
                onChange={event => setName(event.target.value)}
                aria-invalid={errors.name ? true : undefined}
                aria-describedby={errors.name ? "sales-name-error" : undefined}
                required
              />
              {errors.name && <p className="ms-field-error" id="sales-name-error">{errors.name}</p>}
            </div>

            <div>
              <label htmlFor="sales-email">Email</label>
              <input
                id="sales-email"
                name="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={event => setEmail(event.target.value)}
                aria-invalid={errors.email ? true : undefined}
                aria-describedby={errors.email ? "sales-email-error" : undefined}
                required
              />
              {errors.email && <p className="ms-field-error" id="sales-email-error">{errors.email}</p>}
            </div>

            <div>
              <label htmlFor="sales-phone">Phone (optional)</label>
              <input
                id="sales-phone"
                name="phone"
                type="tel"
                autoComplete="tel"
                value={phone}
                onChange={event => setPhone(event.target.value)}
              />
            </div>

            <div>
              <label htmlFor="sales-company">Company (optional)</label>
              <input
                id="sales-company"
                name="company"
                type="text"
                autoComplete="organization"
                value={company}
                onChange={event => setCompany(event.target.value)}
                aria-describedby="sales-company-hint"
              />
              <p id="sales-company-hint">Sole proprietors welcome</p>
            </div>

            <div>
              <label htmlFor="sales-team-size">How many people will use Ringlite?</label>
              <select
                id="sales-team-size"
                name="team_size"
                value={teamSize}
                onChange={event => setTeamSize(event.target.value)}
                aria-invalid={errors.teamSize ? true : undefined}
                aria-describedby={errors.teamSize ? "sales-team-size-error" : undefined}
                required
              >
                <option value="">Choose one</option>
                {TEAM_SIZES.map(size => <option key={size} value={size}>{size}</option>)}
              </select>
              {errors.teamSize && <p className="ms-field-error" id="sales-team-size-error">{errors.teamSize}</p>}
            </div>

            <div>
              <label htmlFor="sales-numbers">How many numbers do you need?</label>
              <select
                id="sales-numbers"
                name="numbers_needed"
                value={numbersNeeded}
                onChange={event => setNumbersNeeded(event.target.value)}
                aria-invalid={errors.numbersNeeded ? true : undefined}
                aria-describedby={errors.numbersNeeded ? "sales-numbers-error" : undefined}
                required
              >
                <option value="">Choose one</option>
                {NUMBER_COUNTS.map(count => <option key={count} value={count}>{count}</option>)}
              </select>
              {errors.numbersNeeded && <p className="ms-field-error" id="sales-numbers-error">{errors.numbersNeeded}</p>}
            </div>

            <div>
              <label htmlFor="sales-switching">What are you switching from?</label>
              <select
                id="sales-switching"
                name="switching_from"
                value={switchingFrom}
                onChange={event => setSwitchingFrom(event.target.value)}
              >
                {SWITCHING_FROM.map(provider => <option key={provider} value={provider}>{provider}</option>)}
              </select>
            </div>

            <div>
              <label htmlFor="sales-message">Anything else we should know? (optional)</label>
              <textarea
                id="sales-message"
                name="message"
                rows={4}
                value={message}
                onChange={event => setMessage(event.target.value)}
              />
            </div>

            <div>
              <label htmlFor="sales-sms">
                <input
                  id="sales-sms"
                  name="sms_consent"
                  type="checkbox"
                  checked={smsConsent}
                  onChange={event => setSmsConsent(event.target.checked)}
                />
                Text me about my enquiry. Message and data rates may apply. Reply STOP to opt out.
              </label>
            </div>

            {status === "failed" && (
              <p role="alert" className="ms-form-error">That didn’t send. Try again in a moment.</p>
            )}

            <button className="rl-button" type="submit" disabled={status === "sending"}>
              {status === "sending" ? "Sending…" : "Send message"}
            </button>
          </form>
        )}
      </section>
    </SitePage>
  );
}

export function FaqPage() {
  const [query, setQuery] = React.useState("");
  const [topic, setTopic] = React.useState<"All" | FaqTopic>("All");

  const needle = query.trim().toLowerCase();
  const matches = FAQS.filter(faq => {
    if (topic !== "All" && faq.topic !== topic) return false;
    if (!needle) return true;
    return (
      faq.q.toLowerCase().includes(needle) ||
      faq.a.toLowerCase().includes(needle) ||
      faq.keywords.some(keyword => keyword.toLowerCase().includes(needle))
    );
  });
  const matchingTopics = FAQ_TOPICS.filter(name => matches.some(faq => faq.topic === name));

  return (
    <SitePage title="Questions and answers" description="Answers about Ringlite plans, numbers, calling and texting.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span /> FAQ</p>
        <h1 className="ms-h1">Before we say hello.</h1>
        <div>
          <label htmlFor="faq-search">Search questions</label>
          <input
            id="faq-search"
            type="search"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="Porting, 10DLC, refunds…"
            autoComplete="off"
          />
        </div>
        <div className="ms-chips" role="group" aria-label="Filter questions by topic">
          <button type="button" aria-pressed={topic === "All"} onClick={() => setTopic("All")}>All</button>
          {FAQ_TOPICS.map(name => (
            <button key={name} type="button" aria-pressed={topic === name} onClick={() => setTopic(name)}>{name}</button>
          ))}
        </div>
      </section>

      <div aria-live="polite">
        {matches.length === 0 ? (
          <p className="rl-wrap">No answers match. Ask the assistant in the corner, or <Link className="rl-text-link" to="/sales">talk to sales</Link>.</p>
        ) : (
          matchingTopics.map(name => (
            <section key={name} className="rl-wrap ms-faq-section">
              <h2>{name}</h2>
              <div className="ms-faq-list">
                {matches.filter(faq => faq.topic === name).map(faq => (
                  <details key={faq.id}>
                    <summary>{faq.q}<Plus size={18} aria-hidden="true" /></summary>
                    <p>{faq.a}</p>
                  </details>
                ))}
              </div>
            </section>
          ))
        )}
      </div>

      <CtaBand title="Still curious?" body="Ask the assistant, or talk to a person." />
    </SitePage>
  );
}

export function SecurityPage() {
  return (
    <SitePage title="Security" description="How Ringlite keeps accounts, numbers and customers safe.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span /> SECURITY</p>
        <h1 className="ms-h1">Trust starts with who’s on the line.</h1>
        <p className="ms-lede">Your numbers, your conversations and your customers’ details. Here is what we do to keep them safe, and what we ask of you.</p>
      </section>

      <section className="ms-cards rl-wrap">
        <article className="ms-card">
          <ShieldCheck size={20} aria-hidden="true" />
          <h3>Identity-verified accounts</h3>
          <p>Every workspace belongs to a person we have verified. It keeps our numbers off spam lists and helps your calls and texts arrive.</p>
        </article>
        <article className="ms-card">
          <KeyRound size={20} aria-hidden="true" />
          <h3>Two-step sign-in</h3>
          <p>Signing in asks for a second factor, so a password on its own cannot open your workspace.</p>
        </article>
        <article className="ms-card">
          <Lock size={20} aria-hidden="true" />
          <h3>Your data stays in your workspace</h3>
          <p>Calls, texts, notes and recordings belong to your workspace. Other Ringlite accounts cannot see them.</p>
        </article>
        <article className="ms-card">
          <ShieldAlert size={20} aria-hidden="true" />
          <h3>Fraud controls</h3>
          <p>New accounts start with a low daily spending limit that rises after the first month. Premium-rate and other high-risk destinations are blocked.</p>
        </article>
        <article className="ms-card">
          <MessageSquare size={20} aria-hidden="true" />
          <h3>Carrier-registered texting</h3>
          <p>Local numbers text through 10DLC brand and campaign registration. We file it for you, show the status, and honour STOP and other opt-out keywords automatically.</p>
        </article>
        <article className="ms-card">
          <PhoneCall size={20} aria-hidden="true" />
          <h3>E911 on every number</h3>
          <p>Every number carries E911, so a 911 call reaches the emergency centre for the address registered on that number. <Link className="rl-text-link" to="/legal/911">Read the 911 disclosure</Link>.</p>
        </article>
        <article className="ms-card">
          <Flag size={20} aria-hidden="true" />
          <h3>Report abuse</h3>
          <p>If a Ringlite number calls or texts you and you do not know why, tell us. We review every report. <Link className="rl-text-link" to="/report">Report abuse</Link>.</p>
        </article>
      </section>

      <p className="ms-note rl-wrap">We don’t hold SOC 2 or HIPAA certification today, and Ringlite should not be used for protected health information.</p>

      <CtaBand title="Questions about security?" />
    </SitePage>
  );
}

export function E911Page() {
  return (
    <SitePage title="911 disclosure" description="How 911 works on Ringlite and its limits.">
      <article className="ms-prose rl-wrap">
        <h1>911 and E911 on Ringlite</h1>
        <p>Last updated September 2026</p>

        <h2>How 911 works</h2>
        <p>Calls to 911 from a Ringlite number go to the emergency centre for the address registered on that number. That address is the only location information the emergency centre receives from us, so it has to be right.</p>

        <h2>Keep your address current</h2>
        <p>Each number has its own emergency address. Keep it up to date in Settings → Numbers, and change it the same day you move desk, office or number. A wrong address can send help to the wrong place.</p>

        <h2>Limits of internet calling</h2>
        <p>Ringlite calls run over the internet, and internet calling has limits:</p>
        <ul>
          <li>There is no service during a power or internet outage.</li>
          <li>The browser or device must be running and signed in.</li>
          <li>Calling from somewhere other than the registered address may reach the wrong emergency centre.</li>
          <li>A call-back may fail if your number is not connected.</li>
        </ul>

        <h2>Tell everyone who uses Ringlite</h2>
        <p>Make sure everyone in your workspace knows these limits, and that each of them has another way to call 911 — a mobile phone or a landline — in case the browser is not available.</p>

        <h2>Where Ringlite works</h2>
        <p>Our calling area is simple: {COVERAGE}.</p>

        <p><Link className="rl-text-link" to="/">Back to home</Link></p>
      </article>
    </SitePage>
  );
}
