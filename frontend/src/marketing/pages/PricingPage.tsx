/**
 * /pricing — the conversion page: a monthly/yearly switch, plan cards, a calculator that finds
 * the cheapest plan for a team and prices the same team at per-seat competitors, the rate
 * card, fair use, and the full feature comparison. Every price and limit comes from
 * @/marketing/pricing.config.
 */
import * as React from "react";
import { Link } from "react-router-dom";
import { ArrowDown, Check, Minus } from "lucide-react";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import {
  CALLS_PER_NUMBER,
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  COVERAGE,
  CUSTOM_FROM_USERS,
  DAILY_OUTBOUND_PER_NUMBER,
  FEATURE_SECTIONS,
  PLANS,
  RATES,
  YEARLY,
  cents,
  competitorCost,
  minutePoolsLine,
  minutesLine,
  missesNumberPrice,
  money,
  monthlyWhenYearly,
  packageLine,
  perBill,
  recommend,
  userLimitLine,
  type Billing,
  type Plan,
} from "@/marketing/pricing.config";

const MAX_USERS = 60;
const MAX_NUMBERS = 40;

function initialBilling(): Billing {
  try {
    return new URLSearchParams(window.location.search).get("billing") === "year" ? "year" : "month";
  } catch {
    return "month";
  }
}

/** The signup link for a plan, carrying the chosen billing through to checkout. */
function ctaHref(plan: Plan, billing: Billing): string {
  if (plan.price === null) return plan.cta.to;
  return billing === "year" ? `${plan.cta.to}&billing=year` : plan.cta.to;
}

export function BillingSwitch({ billing, onChange }: { billing: Billing; onChange: (b: Billing) => void }) {
  return (
    <div className="ms-billing" role="radiogroup" aria-label="Billing">
      <button type="button" role="radio" aria-checked={billing === "month"} onClick={() => onChange("month")}>
        Monthly
      </button>
      <button type="button" role="radio" aria-checked={billing === "year"} onClick={() => onChange("year")}>
        Yearly <em>{YEARLY.label}</em>
      </button>
    </div>
  );
}

function PlanPrice({ plan, billing }: { plan: Plan; billing: Billing }) {
  if (plan.price === null) return <div className="ms-price"><strong className="ms-price-custom">Let's talk</strong></div>;
  if (billing === "month") {
    return (
      <div className="ms-price">
        <strong>{money(plan.price)}</strong>
        <span>per month</span>
      </div>
    );
  }
  return (
    <div className="ms-price">
      <strong>{money(monthlyWhenYearly(plan.price))}</strong>
      <span>per month, billed {money(perBill(plan.price, "year"))} yearly</span>
    </div>
  );
}

function Cell({ value }: { value: boolean | string }) {
  if (value === true) return <Check size={17} className="ms-yes" aria-label="Included" />;
  if (value === false) return <Minus size={17} className="ms-no" aria-label="Not included" />;
  return <>{value}</>;
}

export function PricingPage() {
  const [billing, setBilling] = React.useState<Billing>(initialBilling);
  const [users, setUsers] = React.useState(5);
  const [numbers, setNumbers] = React.useState(3);

  const q = recommend(users, numbers);
  const ours = q.monthly === null ? null : billing === "year" ? monthlyWhenYearly(q.monthly) : q.monthly;
  const barRows = [
    ...(ours !== null ? [{ label: `Ringlite ${q.plan.name}`, value: ours, isUs: true, partial: false }] : []),
    ...COMPETITOR_SEAT_PRICES.map(c => ({
      label: c.name,
      value: competitorCost(c, users, numbers, billing),
      isUs: false,
      partial: missesNumberPrice(c, users, numbers),
    })),
  ];
  const barMax = Math.max(...barRows.map(row => row.value), 1);
  const anyPartial = barRows.some(row => row.partial);

  const scrollToCompare = () => {
    const target = document.getElementById("compare");
    if (!target) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  };

  return (
    <SitePage title="Pricing" description="Simple plans with your team and phone numbers included. Pay monthly, or yearly and get two months free.">
      <section className="ms-page-hero ms-pricing-hero rl-wrap">
        <p className="rl-eyebrow"><span /> PRICING</p>
        <h1 className="ms-h1">Your team and your numbers. <span>One simple price.</span></h1>
        <p className="ms-lede">
          Every plan includes users and phone numbers, and you can add more of either any time. Team
          and Business include call minutes the whole team shares ({minutePoolsLine()} a month), with
          no "unlimited" small print.
        </p>
        <BillingSwitch billing={billing} onChange={setBilling} />
      </section>

      <div className="ms-plans ms-plans-4 rl-wrap">
        {PLANS.map(plan => (
          <article key={plan.code} className={"ms-plan" + (plan.highlight ? " is-highlight" : "")}>
            <div className="ms-plan-tag">
              <span className="rl-mono">{plan.name}</span>
              {plan.highlight && <span className="ms-popular">MOST POPULAR</span>}
            </div>
            <p className="ms-tagline">{plan.tagline}</p>
            <PlanPrice plan={plan} billing={billing} />
            <ul className="ms-includes">
              <li>{packageLine(plan)}</li>
              <li>{userLimitLine(plan)}</li>
              <li>{minutesLine(plan)}</li>
              {plan.extraUser !== null && plan.extraNumber !== null && (
                <li>
                  Extra user {money(perBill(plan.extraUser, billing))} · number {money(perBill(plan.extraNumber, billing))}
                  {billing === "year" ? " a year" : " a month"}
                </li>
              )}
            </ul>
            <Link className={plan.highlight ? "rl-button" : "ms-ghost"} to={ctaHref(plan, billing)}>
              {plan.cta.label}
            </Link>
          </article>
        ))}
      </div>
      <div className="rl-wrap ms-compare-jump">
        <button type="button" className="ms-ghost" onClick={scrollToCompare}>
          Compare all features <ArrowDown size={16} aria-hidden="true" />
        </button>
      </div>

      <section className="ms-band ms-band-dark" aria-labelledby="calc-h">
        <div className="ms-calc rl-wrap rl-reveal">
          <p className="rl-eyebrow"><span /> CALCULATOR</p>
          <h2 id="calc-h">What your team would pay</h2>
          <div className="ms-calc-grid">
            <div className="ms-calc-inputs">
              <label htmlFor="calc-users">
                <span>People <output htmlFor="calc-users">{users}</output></span>
                <input id="calc-users" type="range" min={1} max={MAX_USERS} value={users}
                  onChange={event => setUsers(Number(event.target.value))} />
              </label>
              <label htmlFor="calc-numbers">
                <span>Phone numbers <output htmlFor="calc-numbers">{numbers}</output></span>
                <input id="calc-numbers" type="range" min={1} max={MAX_NUMBERS} value={numbers}
                  onChange={event => setNumbers(Number(event.target.value))} />
              </label>
              <BillingSwitch billing={billing} onChange={setBilling} />
            </div>
            <div className="ms-calc-result" aria-live="polite">
              <p className="ms-calc-plan rl-mono">Best fit: {q.plan.name}</p>
              {q.monthly !== null && ours !== null ? (
                <>
                  <p className="ms-calc-total">
                    {money(ours)}<small> /mo + usage</small>
                  </p>
                  {billing === "year" && <p className="ms-calc-billed">Billed {money(perBill(q.monthly, "year"))} a year</p>}
                  <ul className="ms-calc-lines">
                    <li>{q.plan.name} {money(q.plan.price ?? 0)}: {packageLine(q.plan)}</li>
                    {q.extraUsers > 0 && (
                      <li>{q.extraUsers} extra {q.extraUsers === 1 ? "user" : "users"} × {money(q.plan.extraUser ?? 0)}</li>
                    )}
                    {q.extraNumbers > 0 && (
                      <li>{q.extraNumbers} extra {q.extraNumbers === 1 ? "number" : "numbers"} × {money(q.plan.extraNumber ?? 0)}</li>
                    )}
                    <li>{minutesLine(q.plan)}</li>
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
          <p className="ms-bars-title">The same {users} {users === 1 ? "person" : "people"} and {numbers} {numbers === 1 ? "number" : "numbers"} elsewhere, per month</p>
          <div className="ms-bars">
            {barRows.map(row => (
              <div className="ms-bar-row" key={row.label}>
                <span className="ms-bar-label">{row.label}</span>
                <div className="ms-bar-track" aria-hidden="true">
                  <div className={"ms-bar-fill" + (row.isUs ? " is-us" : "")} style={{ transform: `scaleX(${row.value / barMax})` }} />
                </div>
                <span className="ms-bar-amount">{money(Math.round(row.value))}/mo{row.partial ? "*" : ""}</span>
              </div>
            ))}
          </div>
          <p className="ms-footnote">
            List prices, {billing === "year" ? "paid yearly where the vendor offers it" : "paid monthly"}, {COMPETITORS_CHECKED}:
            each seat at its plan price with that vendor's minimum seats, plus phone numbers beyond the
            one each seat includes.{anyPartial ? " * That vendor does not publish its extra-number price, so extra numbers are left out of its total." : ""}{" "}
            Competitors bundle "unlimited" calling under fair use policies; Ringlite includes an exact
            pool and then charges {cents(RATES.minute)}/min, so compare usage too.
          </p>
        </div>
      </section>

      <section className="ms-rates rl-wrap rl-reveal" aria-labelledby="rates-h">
        <h2 id="rates-h">The rate card</h2>
        <dl className="ms-rates-list">
          <div><dt>Call minutes included</dt><dd>{minutePoolsLine()} a month, shared</dd></div>
          <div><dt>Calls per minute after that</dt><dd>{cents(RATES.minute)}</dd></div>
          <div><dt>Text per segment</dt><dd>{cents(RATES.text)}</dd></div>
          <div><dt>Picture message</dt><dd>{cents(RATES.picture)}</dd></div>
          <div><dt>Fax per page</dt><dd>{cents(RATES.faxPage)}</dd></div>
          <div><dt>Minute bundle</dt><dd>{RATES.minuteBundle.units.toLocaleString()} min for {money(RATES.minuteBundle.price)}</dd></div>
          <div><dt>Text bundle</dt><dd>{RATES.textBundle.units.toLocaleString()} texts for {money(RATES.textBundle.price)}</dd></div>
          <div><dt>Coverage</dt><dd>{COVERAGE}</dd></div>
        </dl>
      </section>

      <section className="ms-band" aria-labelledby="fairuse-h">
        <div className="ms-fairuse rl-wrap rl-reveal">
          <h2 id="fairuse-h">Fair use, in plain words</h2>
          <div className="ms-fairuse-grid">
            <article className="ms-card">
              <h3>Exact minutes, then pay as you go</h3>
              <p>
                Team and Business include one pool of call minutes the whole team shares
                ({minutePoolsLine()} a month). Past the pool, and on Starter, calls are {cents(RATES.minute)} a
                minute and texts {cents(RATES.text)} a segment, from a prepaid balance you top up.
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
                agreed to hear from you.
              </p>
            </article>
          </div>
        </div>
      </section>

      <section id="compare" className="ms-matrix-section rl-wrap" aria-labelledby="compare-h">
        <div className="ms-matrix">
          <div className="ms-matrix-head">
            <div className="ms-matrix-title">
              <h2 id="compare-h">Compare plans</h2>
              <BillingSwitch billing={billing} onChange={setBilling} />
            </div>
            {PLANS.map(plan => (
              <div key={plan.code} className={"ms-matrix-plan" + (plan.highlight ? " is-highlight" : "")}>
                {plan.highlight && <span className="ms-matrix-popular">Most popular</span>}
                <strong>{plan.name}</strong>
                <small>
                  {plan.price === null
                    ? "Custom pricing"
                    : billing === "year"
                      ? `${money(monthlyWhenYearly(plan.price))}/mo, billed yearly`
                      : `${money(plan.price)} per month`}
                </small>
                <Link className={plan.highlight ? "rl-button" : "ms-ghost"} to={ctaHref(plan, billing)}>
                  {plan.price === null ? "Talk to sales" : "Get started"}
                </Link>
              </div>
            ))}
          </div>
          {FEATURE_SECTIONS.map(section => (
            <table className="ms-matrix-table" key={section.title}>
              <caption>{section.title}</caption>
              <tbody>
                {section.rows.map(row => (
                  <tr key={row.label}>
                    <th scope="row">
                      {row.label}
                      {row.note && <small>{row.note}</small>}
                    </th>
                    {(typeof row.values === "function" ? row.values(billing) : row.values).map((value, i) => (
                      <td key={PLANS[i].code}><Cell value={value} /></td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
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
