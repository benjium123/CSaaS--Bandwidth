/**
 * /pricing — the conversion page: plan cards, a live calculator that compares Ringlite's
 * per-line price with per-seat competitors, the full plan comparison, the rate card and the
 * fair-use rules. Every price, allowance and limit comes from @/marketing/pricing.config.
 */
import * as React from "react";
import { Link } from "react-router-dom";
import { Check } from "lucide-react";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  COVERAGE,
  DAILY_OUTBOUND_PER_NUMBER,
  PLANS,
  RATES,
  YEARLY_SAVING_LABEL,
  cents,
  money,
  recommend,
  startingPrice,
} from "@/marketing/pricing.config";
import type { Billing, Plan } from "@/marketing/pricing.config";

/** Slider bounds: the calculator tops out at the largest plan's user count and 20 lines. */
const MAX_USERS = PLANS[PLANS.length - 1].users.max;
const MAX_NUMBERS = 20;

/**
 * Every feature string any plan lists, in plan order, without the "Everything in …" roll-ups
 * (those are expanded into the rows underneath them).
 */
const COMPARED_FEATURES = Array.from(new Set(PLANS.flatMap(plan => plan.features))).filter(
  feature => !feature.startsWith("Everything in"),
);

/** A plan carries a feature when it lists it, or when it rolls up every plan before it. */
function hasFeature(planIndex: number, feature: string): boolean {
  const plan = PLANS[planIndex];
  const inherits = plan.features.some(line => line.startsWith("Everything in"));
  const sources = inherits ? PLANS.slice(0, planIndex + 1) : [plan];
  return sources.some(source => source.features.includes(feature));
}

function usersSummary(plan: Plan): string {
  const included = `${plan.users.included} ${plan.users.included === 1 ? "user" : "users"} included`;
  const upTo = plan.users.max > plan.users.included ? `, up to ${plan.users.max}` : "";
  const extra = plan.extraUserPrice !== null ? ` · extra ${money(plan.extraUserPrice)}/user` : "";
  return `${included}${upTo}${extra}`;
}

function allowanceSummary(plan: Plan): string {
  if (!plan.perNumberAllowance) {
    return `Pay as you go · ${cents(RATES.minute)}/min · ${cents(RATES.text)}/text`;
  }
  const { minutes, texts } = plan.perNumberAllowance;
  return `${minutes.toLocaleString()} min + ${texts.toLocaleString()} texts per number, pooled`;
}

function callsAtOnceByPlan(): string {
  return PLANS.map(plan => `${plan.callsPerNumber} on ${plan.name}`).join(", ");
}

export function PricingPage() {
  const [billing, setBilling] = React.useState<Billing>("yearly");
  const [users, setUsers] = React.useState(5);
  const [numbers, setNumbers] = React.useState(2);

  const q = recommend(users, numbers, billing);
  const price = q.plan.pricePerNumber[billing];
  const barRows = [
    { label: `Ringlite (${q.plan.name})`, value: q.monthly, isUs: true },
    ...COMPETITOR_SEAT_PRICES.map(competitor => ({
      label: competitor.name,
      value: Math.max(users, competitor.minSeats) * competitor[billing],
      isUs: false,
    })),
  ];
  const barMax = Math.max(...barRows.map(row => row.value), 1);

  return (
    <SitePage title="Pricing" description="Plans priced per phone number with your team included.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span /> PRICING · PER LINE, NOT PER PERSON</p>
        <h1 className="ms-h1">Pay for your lines. <span>Bring your team.</span></h1>
        <p className="ms-lede">
          Every plan includes your team, and every number you add brings its pooled minutes and texts
          with it. You pay for the lines you use, not for the people who answer them.
        </p>
        <div className="ms-seg" role="group" aria-label="Billing period">
          <button type="button" aria-pressed={billing === "monthly"} onClick={() => setBilling("monthly")}>
            Monthly
          </button>
          <button type="button" aria-pressed={billing === "yearly"} onClick={() => setBilling("yearly")}>
            Yearly <small>{YEARLY_SAVING_LABEL}</small>
          </button>
        </div>
      </section>

      <div className="ms-plans rl-wrap">
        {PLANS.map(plan => (
          <article key={plan.code} className={"ms-plan" + (plan.highlight ? " is-highlight" : "")}>
            <div className="ms-plan-tag">
              <span className="rl-mono">{plan.name}</span>
              {plan.highlight && <span className="ms-popular">MOST POPULAR</span>}
            </div>
            <p className="ms-tagline">{plan.tagline}</p>
            <div className="ms-price">
              <strong>{money(plan.pricePerNumber[billing])}</strong>
              <span>per number<br />per month</span>
            </div>
            {plan.numbers.min > 1 && (
              <p className="ms-starts">
                Starts at {money(startingPrice(plan, billing))}/mo with {plan.numbers.min} numbers
              </p>
            )}
            <ul className="ms-includes">
              <li>{usersSummary(plan)}</li>
              <li>{allowanceSummary(plan)}</li>
              <li>
                {plan.callsPerNumber} {plan.callsPerNumber === 1 ? "call" : "calls"} at once per number
              </li>
            </ul>
            <ul className="ms-features">
              {plan.features.map(feature => (
                <li key={feature}>
                  <Check size={15} aria-hidden="true" />
                  {feature}
                </li>
              ))}
            </ul>
            <Link className={plan.highlight ? "rl-button" : "ms-ghost"} to={plan.cta.to}>
              {plan.cta.label}
            </Link>
          </article>
        ))}
      </div>

      <section className="ms-calc rl-wrap rl-reveal" aria-labelledby="calc-h">
        <h2 id="calc-h">What your team would pay</h2>
        <div className="ms-calc-grid">
          <div className="ms-calc-inputs">
            <label htmlFor="calc-users">
              People <output htmlFor="calc-users">{users}</output>
            </label>
            <input
              id="calc-users"
              type="range"
              min={1}
              max={MAX_USERS}
              value={users}
              onChange={event => setUsers(Number(event.target.value))}
            />
            <label htmlFor="calc-numbers">
              Phone numbers <output htmlFor="calc-numbers">{numbers}</output>
            </label>
            <input
              id="calc-numbers"
              type="range"
              min={1}
              max={MAX_NUMBERS}
              value={numbers}
              onChange={event => setNumbers(Number(event.target.value))}
            />
          </div>
          <div className="ms-calc-result" aria-live="polite">
            <p className="ms-calc-plan rl-mono">{q.plan.name}</p>
            <p className="ms-calc-total">
              <strong>{money(q.monthly)}</strong>
              <span>/mo</span>
            </p>
            <ul className="ms-calc-lines">
              <li>{q.numbers} numbers × {money(price)}</li>
              {q.extraUsers > 0 && (
                <li>{q.extraUsers} extra users × {money(q.plan.extraUserPrice ?? 0)}</li>
              )}
              <li>
                {q.poolMinutes !== null && q.poolTexts !== null
                  ? `${q.poolMinutes.toLocaleString()} min + ${q.poolTexts.toLocaleString()} texts`
                  : "Pay as you go"}
              </li>
              <li>{q.callsAtOnce} {q.callsAtOnce === 1 ? "call" : "calls"} at once</li>
            </ul>
            {q.blocked && (
              <p className="ms-calc-blocked">
                {q.blocked}. <Link className="rl-text-link" to="/sales">Talk to sales</Link>
              </p>
            )}
          </div>
        </div>
        <div className="ms-bars">
          {barRows.map(row => (
            <div className="ms-bar-row" key={row.label}>
              <span className="ms-bar-label">{row.label}</span>
              <div className="ms-bar-track" aria-hidden="true">
                <div
                  className={"ms-bar-fill" + (row.isUs ? " is-us" : "")}
                  style={{ transform: `scaleX(${row.value / barMax})` }}
                />
              </div>
              <span className="ms-bar-amount">{money(row.value)}/mo</span>
            </div>
          ))}
        </div>
        <p className="ms-footnote">
          Competitor list prices, {COMPETITORS_CHECKED}. They advertise unlimited US calling under fair
          use policies. Ringlite includes the pooled minutes shown, then {cents(RATES.minute)}/min.
        </p>
      </section>

      <section className="rl-wrap rl-reveal" aria-labelledby="compare-h">
        <h2 id="compare-h">Compare plans</h2>
        <div className="ms-table-wrap">
          <table className="ms-table">
            <thead>
              <tr>
                <th scope="col">Plan</th>
                {PLANS.map(plan => (
                  <th key={plan.code} scope="col">{plan.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Price per number ({billing})</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{money(plan.pricePerNumber[billing])}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Numbers</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.numbers.min}–{plan.numbers.max}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Users included</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.users.included}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Max users</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.users.max}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Extra user</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.extraUserPrice !== null ? money(plan.extraUserPrice) : "—"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Minutes per number</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>
                    {plan.perNumberAllowance ? plan.perNumberAllowance.minutes.toLocaleString() : "Pay as you go"}
                  </td>
                ))}
              </tr>
              <tr>
                <th scope="row">Texts per number</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>
                    {plan.perNumberAllowance ? plan.perNumberAllowance.texts.toLocaleString() : "Pay as you go"}
                  </td>
                ))}
              </tr>
              <tr>
                <th scope="row">Calls at once per number</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.callsPerNumber}</td>
                ))}
              </tr>
              {COMPARED_FEATURES.map(feature => (
                <tr key={feature}>
                  <th scope="row">{feature}</th>
                  {PLANS.map((plan, index) => (
                    <td key={plan.code}>
                      {hasFeature(index, feature)
                        ? <Check size={15} aria-label="Included" />
                        : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="ms-rates rl-wrap rl-reveal" aria-labelledby="rates-h">
        <h2 id="rates-h">The rate card</h2>
        <dl className="ms-rates-list">
          <div><dt>Calls per minute</dt><dd>{cents(RATES.minute)}</dd></div>
          <div><dt>Text per segment</dt><dd>{cents(RATES.text)}</dd></div>
          <div><dt>Picture message</dt><dd>{cents(RATES.picture)}</dd></div>
          <div><dt>Fax per page</dt><dd>{cents(RATES.faxPage)}</dd></div>
          <div>
            <dt>Minute bundle</dt>
            <dd>{RATES.minuteBundle.units.toLocaleString()} min for {money(RATES.minuteBundle.price)}</dd>
          </div>
          <div>
            <dt>Text bundle</dt>
            <dd>{RATES.textBundle.units.toLocaleString()} texts for {money(RATES.textBundle.price)}</dd>
          </div>
          <div><dt>Texting registration</dt><dd>One-time, shown as a total at checkout</dd></div>
          <div><dt>Coverage</dt><dd>{COVERAGE}</dd></div>
        </dl>
      </section>

      <section className="ms-fairuse rl-wrap rl-reveal" aria-labelledby="fairuse-h">
        <h2 id="fairuse-h">Fair use, in plain words</h2>
        <div className="ms-fairuse-grid">
          <article className="ms-card">
            <h3>Pooled per number</h3>
            <p>
              Every number adds its minutes and texts to one pool your whole workspace shares. A quiet
              line's allowance is never wasted; a busy one can spend it.
            </p>
          </article>
          <article className="ms-card">
            <h3>Billed, never cut off</h3>
            <p>
              When the pool runs out, calls continue at {cents(RATES.minute)} a minute and texts at
              {" "}{cents(RATES.text)} a segment, or you can buy a bundle. We email you at 80% and 100%
              of your pool and never stop your service for going over.
            </p>
          </article>
          <article className="ms-card">
            <h3>Numbers stay healthy</h3>
            <p>
              Each number carries a fixed number of live calls at once ({callsAtOnceByPlan()}) and about
              {" "}{DAILY_OUTBOUND_PER_NUMBER} outbound calls a day. Teams that call all day give each
              caller their own number.
            </p>
          </article>
          <article className="ms-card">
            <h3>Not allowed</h3>
            <p>
              Predictive dialers, robocalls, call centers, reselling, and texting people who have not
              agreed to hear from you. Ringlite is for conversations between a business and its
              customers.
            </p>
          </article>
        </div>
      </section>

      <section className="rl-wrap rl-reveal ms-faq-section" aria-labelledby="pricing-faq-h">
        <h2 id="pricing-faq-h">Pricing questions</h2>
        <FaqList topic="Plans and billing" />
      </section>

      <CtaBand title="Pick a number. Bring your team." />
    </SitePage>
  );
}
