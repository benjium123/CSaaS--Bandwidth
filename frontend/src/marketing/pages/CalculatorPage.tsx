/**
 * /calculator — the standalone cost calculator: the Ringlite team calculator, the rules behind
 * the numbers, and the same team priced at each per-seat vendor. Every price comes from
 * @/marketing/pricing.config.
 */
import * as React from "react";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import { TeamCostCalculator } from "@/marketing/TeamCostCalculator";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  RATES,
  cents,
  competitorCost,
  competitorTier,
  minutePoolsLine,
  money,
  planByCode,
  recommend,
  type Billing,
} from "@/marketing/pricing.config";

const TABLE_SIZES = [3, 5, 10, 15];

export function CalculatorPage() {
  const [billing, setBilling] = React.useState<Billing>("month");
  const team = planByCode("team");
  const business = planByCode("business");

  return (
    <SitePage title="Business phone cost calculator" description="Price your team on Ringlite and compare the same team at Quo, RingCentral, Aircall, KrispCall and CallHippo, using published list prices.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />CALCULATOR</p>
        <h1 className="ms-h1">What does a business phone system cost for your team?</h1>
        <p className="ms-lede">
          Set your team size and your phone numbers and we price them on the cheapest Ringlite plan
          that fits, then price the same team at per-seat vendors. Every figure is a published list
          price, checked {COMPETITORS_CHECKED}.
        </p>
      </section>

      <TeamCostCalculator billing={billing} onBillingChange={setBilling} initialUsers={5} initialNumbers={5} />

      <section className="ms-textblock rl-wrap rl-reveal" aria-labelledby="how-h">
        <h2 id="how-h">How we calculate</h2>
        <p>
          Ringlite prices the whole team on one plan. We take the plan's package price, then its
          add-on price for each person and each number past what the package includes, and we show the
          cheapest plan that fits your team.
        </p>
        <p>
          Per-seat tools charge for every person. For each vendor we take your team size, apply their
          minimum number of seats, multiply by their seat price, then add the phone numbers beyond the
          ones the seats include. That covers {COMPETITOR_SEAT_PRICES.length} vendors at their
          published list prices.
        </p>
        <p>
          Usage is extra on both sides. Ringlite Team and Business include {minutePoolsLine()} call
          minutes a month, shared by the whole team. Past the pool, calls are {cents(RATES.minute)} a
          minute and texts are {cents(RATES.text)} each, from a prepaid balance you top up.
        </p>
        <p>
          Add-ons follow the plan: on {team.name} an extra person is {money(team.extraUser ?? 0)} and
          an extra number is {money(team.extraNumber ?? 0)} a month. On {business.name} they are
          {" "}{money(business.extraUser ?? 0)} and {money(business.extraNumber ?? 0)}.
        </p>
      </section>

      <section className="ms-matrix-section rl-wrap" aria-labelledby="cost-table-h">
        <h2 id="cost-table-h">The same team at list prices</h2>
        <table className="ms-matrix-table">
          <caption>Monthly list prices before usage, checked {COMPETITORS_CHECKED}.</caption>
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col">Ringlite</th>
              {COMPETITOR_SEAT_PRICES.map(c => (
                <th scope="col" key={c.slug}>{c.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {TABLE_SIZES.map(n => {
              const r = recommend(n, n);
              return (
                <tr key={n}>
                  <th scope="row">{n} people, {n} numbers</th>
                  <td>{r.plan.name} {r.monthly !== null ? money(r.monthly) : "Custom"}</td>
                  {COMPETITOR_SEAT_PRICES.map(c => {
                    const tier = competitorTier(c, n);
                    return (
                      <td key={c.slug}>
                        {tier.name !== c.name && <small>{tier.name} </small>}
                        {money(Math.round(competitorCost(c, n, n)))}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="ms-faq-section rl-wrap rl-reveal" aria-labelledby="calc-faq-h">
        <h2 id="calc-faq-h">Pricing questions</h2>
        <FaqList ids={["per-number", "overage", "unlimited", "rates", "taxes", "yearly"]} />
      </section>

      <CtaBand title="Price your team, then start calling." />
    </SitePage>
  );
}
