/**
 * /pricing — the conversion page: plan cards, a calculator that finds the cheapest plan for a
 * team and compares it with per-seat competitors, the full plan comparison, the rate card and
 * the fair-use rules. Every price and limit comes from @/marketing/pricing.config.
 */
import * as React from "react";
import { Link } from "react-router-dom";
import { Check } from "lucide-react";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import {
  CALLS_PER_NUMBER,
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  COVERAGE,
  CUSTOM_FROM_USERS,
  DAILY_OUTBOUND_PER_NUMBER,
  MINUTES_PER_USER,
  PLANS,
  RATES,
  addOnLine,
  cents,
  minutesLine,
  money,
  packageLine,
  recommend,
} from "@/marketing/pricing.config";

const MAX_USERS = 60;
const MAX_NUMBERS = 40;

/** Every feature string any plan lists, without the "Everything in ..." roll-ups. */
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

const USAGE_LINE = `Then calls ${cents(RATES.minute)}/min · texts ${cents(RATES.text)}`;

export function PricingPage() {
  const [users, setUsers] = React.useState(5);
  const [numbers, setNumbers] = React.useState(3);

  const q = recommend(users, numbers);
  const barRows = [
    ...(q.monthly !== null ? [{ label: `Ringlite ${q.plan.name}`, value: q.monthly, isUs: true }] : []),
    ...COMPETITOR_SEAT_PRICES.map(competitor => ({
      label: competitor.name,
      value: Math.max(users, competitor.minSeats) * competitor.monthly,
      isUs: false,
    })),
  ];
  const barMax = Math.max(...barRows.map(row => row.value), 1);

  return (
    <SitePage title="Pricing" description="Simple monthly plans with your team and phone numbers included.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span /> PRICING</p>
        <h1 className="ms-h1">Your team and your numbers. <span>One simple price.</span></h1>
        <p className="ms-lede">
          Every plan includes users and phone numbers, and you can add more of either any time. Each
          user brings {MINUTES_PER_USER} call minutes a month, shared by the whole team. After that,
          calls are {cents(RATES.minute)} a minute and texts {cents(RATES.text)}, with no "unlimited"
          small print.
        </p>
      </section>

      <div className="ms-plans ms-plans-4 rl-wrap">
        {PLANS.map(plan => (
          <article key={plan.code} className={"ms-plan" + (plan.highlight ? " is-highlight" : "")}>
            <div className="ms-plan-tag">
              <span className="rl-mono">{plan.name}</span>
              {plan.highlight && <span className="ms-popular">MOST POPULAR</span>}
            </div>
            <p className="ms-tagline">{plan.tagline}</p>
            <div className="ms-price">
              {plan.price !== null ? (
                <>
                  <strong>{money(plan.price)}</strong>
                  <span>per month</span>
                </>
              ) : (
                <strong className="ms-price-custom">Let's talk</strong>
              )}
            </div>
            <ul className="ms-includes">
              <li>{packageLine(plan)}</li>
              {addOnLine(plan) && <li>{addOnLine(plan)}</li>}
              <li>{minutesLine(plan)}</li>
              <li>{plan.price !== null ? USAGE_LINE : "Volume rates on calls and texts"}</li>
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
              <span>People <output htmlFor="calc-users">{users}</output></span>
              <input
                id="calc-users"
                type="range"
                min={1}
                max={MAX_USERS}
                value={users}
                onChange={event => setUsers(Number(event.target.value))}
              />
            </label>
            <label htmlFor="calc-numbers">
              <span>Phone numbers <output htmlFor="calc-numbers">{numbers}</output></span>
              <input
                id="calc-numbers"
                type="range"
                min={1}
                max={MAX_NUMBERS}
                value={numbers}
                onChange={event => setNumbers(Number(event.target.value))}
              />
            </label>
          </div>
          <div className="ms-calc-result" aria-live="polite">
            <p className="ms-calc-plan rl-mono">Best fit: {q.plan.name}</p>
            {q.monthly !== null ? (
              <>
                <p className="ms-calc-total">
                  {money(q.monthly)}<small> /mo + usage</small>
                </p>
                <ul className="ms-calc-lines">
                  <li>{q.plan.name} {money(q.plan.price ?? 0)}: {packageLine(q.plan)}</li>
                  {q.extraUsers > 0 && (
                    <li>
                      {q.extraUsers} extra {q.extraUsers === 1 ? "user" : "users"} × {money(q.plan.extraUser ?? 0)}
                    </li>
                  )}
                  {q.extraNumbers > 0 && (
                    <li>
                      {q.extraNumbers} extra {q.extraNumbers === 1 ? "number" : "numbers"} × {money(q.plan.extraNumber ?? 0)}
                    </li>
                  )}
                  <li>{(q.users * MINUTES_PER_USER).toLocaleString("en-US")} call minutes a month, shared</li>
                  <li>Up to {q.callsAtOnce} {q.callsAtOnce === 1 ? "call" : "calls"} at once</li>
                </ul>
              </>
            ) : (
              <p className="ms-calc-blocked">
                More than {CUSTOM_FROM_USERS} people? We'll put together a plan for you.{" "}
                <Link className="rl-text-link" to="/sales?plan=custom">Talk to sales</Link>
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
          Monthly list prices per user, {COMPETITORS_CHECKED}. Competitors bundle "unlimited" US calling
          under fair use policies; Ringlite includes {MINUTES_PER_USER} minutes per user and then charges
          {cents(RATES.minute)}/min, so compare usage too if your team is on the phone all day.
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
                <th scope="row">Price per month</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.price !== null ? money(plan.price) : "Custom"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Users included</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.included ? plan.included.users : "Custom"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Numbers included</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.included ? plan.included.numbers : "Custom"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Extra user</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.extraUser !== null ? `${money(plan.extraUser)}/mo` : "Custom"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Extra number</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>{plan.extraNumber !== null ? `${money(plan.extraNumber)}/mo` : "Custom"}</td>
                ))}
              </tr>
              <tr>
                <th scope="row">Call minutes included</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>
                    {plan.included ? `${(plan.included.users * MINUTES_PER_USER).toLocaleString("en-US")}/mo, shared` : "Custom"}
                  </td>
                ))}
              </tr>
              <tr>
                <th scope="row">Calls and texts after that</th>
                {PLANS.map(plan => (
                  <td key={plan.code}>
                    {plan.price !== null ? `${cents(RATES.minute)}/min · ${cents(RATES.text)}/text` : "Volume rates"}
                  </td>
                ))}
              </tr>
              {COMPARED_FEATURES.map(feature => (
                <tr key={feature}>
                  <th scope="row">{feature}</th>
                  {PLANS.map((plan, index) => (
                    <td key={plan.code}>
                      {hasFeature(index, feature) ? <Check size={15} aria-label="Included" /> : "—"}
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
          <div><dt>Call minutes included</dt><dd>{MINUTES_PER_USER} per user a month, shared</dd></div>
          <div><dt>Calls per minute after that</dt><dd>{cents(RATES.minute)}</dd></div>
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
            <h3>Exact minutes, then pay as you go</h3>
            <p>
              Every user adds {MINUTES_PER_USER} call minutes a month to one shared pool. Past the pool,
              calls are {cents(RATES.minute)} a minute and texts {cents(RATES.text)} a segment, taken
              from a prepaid balance you top up. Bundles bring the price down if you use a lot.
            </p>
          </article>
          <article className="ms-card">
            <h3>No surprise bills</h3>
            <p>
              You see your balance before you spend it, with alerts when it runs low. New workspaces
              have a daily spending limit that protects you if a card or password is ever stolen.
            </p>
          </article>
          <article className="ms-card">
            <h3>Numbers stay healthy</h3>
            <p>
              Each number carries up to {CALLS_PER_NUMBER} live calls at once and about
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

      <CtaBand title="Pick a plan. Bring your team." />
    </SitePage>
  );
}
