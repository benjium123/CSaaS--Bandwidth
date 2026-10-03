/**
 * The team cost calculator: two sliders for people and phone numbers, the cheapest Ringlite
 * plan for that team, and the same team priced at per-seat competitors. It is used on
 * /pricing and /calculator. Every price and limit comes from @/marketing/pricing.config.
 */
import * as React from "react";
import { Link } from "react-router-dom";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  CUSTOM_FROM_USERS,
  RATES,
  YEARLY,
  cents,
  competitorCost,
  competitorTier,
  minutesLine,
  missesNumberPrice,
  money,
  monthlyWhenYearly,
  packageLine,
  perBill,
  recommend,
  type Billing,
} from "@/marketing/pricing.config";

const MAX_USERS = 60;
const MAX_NUMBERS = 40;

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

export function TeamCostCalculator({ billing, onBillingChange, initialUsers = 5, initialNumbers = 3, headingLevel = 2 }: {
  billing: Billing;
  onBillingChange: (b: Billing) => void;
  initialUsers?: number;
  initialNumbers?: number;
  headingLevel?: 2 | 3;
}) {
  const [users, setUsers] = React.useState(initialUsers);
  const [numbers, setNumbers] = React.useState(initialNumbers);

  const q = recommend(users, numbers);
  const ours = q.monthly === null ? null : billing === "year" ? monthlyWhenYearly(q.monthly) : q.monthly;
  const barRows = [
    ...(ours !== null ? [{ label: `Ringlite ${q.plan.name}`, value: ours, isUs: true, partial: false }] : []),
    ...COMPETITOR_SEAT_PRICES.map(c => ({
      label: competitorTier(c, users).name,
      value: competitorCost(c, users, numbers, billing),
      isUs: false,
      partial: missesNumberPrice(c, users, numbers),
    })),
  ];
  const barMax = Math.max(...barRows.map(row => row.value), 1);
  const anyPartial = barRows.some(row => row.partial);

  return (
    <section className="ms-band ms-band-dark" aria-labelledby="calc-h">
      <div className="ms-calc rl-wrap rl-reveal">
        <p className="rl-eyebrow"><span /> CALCULATOR</p>
        {headingLevel === 3
          ? <h3 id="calc-h">What your team would pay</h3>
          : <h2 id="calc-h">What your team would pay</h2>}
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
            <BillingSwitch billing={billing} onChange={onBillingChange} />
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
              <span className="ms-bar-amount">{money(row.isUs ? row.value : Math.round(row.value))}/mo{row.partial ? "*" : ""}</span>
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
  );
}
