import * as React from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowUpRight, Check, Minus, Plus } from "lucide-react";
import { SitePage, FaqList, CtaBand } from "@/marketing/SiteChrome";
import { COMPETITORS, type Competitor } from "@/marketing/content";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  competitorCost,
  competitorTier,
  minutePoolsLine,
  missesNumberPrice,
  money,
  recommend,
} from "@/marketing/pricing.config";

/** A small − n + control for the same-team comparison. */
function Stepper({ label, value, min, max, onChange }: {
  label: string; value: number; min: number; max: number; onChange: (n: number) => void;
}) {
  return (
    <div className="ms-stepper">
      <span>{label}</span>
      <button type="button" aria-label={`Fewer ${label.toLowerCase()}`} disabled={value <= min} onClick={() => onChange(value - 1)}>
        <Minus size={15} aria-hidden="true" />
      </button>
      <output aria-live="polite">{value}</output>
      <button type="button" aria-label={`More ${label.toLowerCase()}`} disabled={value >= max} onClick={() => onChange(value + 1)}>
        <Plus size={15} aria-hidden="true" />
      </button>
    </div>
  );
}

export function ComparePage() {
  const { slug } = useParams<{ slug: string }>();
  const [users, setUsers] = React.useState(5);
  const [numbers, setNumbers] = React.useState(5);
  const competitor: Competitor | undefined = COMPETITORS.find(c => c.slug === slug);
  if (!competitor) return <Navigate to="/" replace />;
  const c = competitor;

  const r = recommend(users, numbers);
  const price = COMPETITOR_SEAT_PRICES.find(e => e.slug === c.slug);
  const theirs = price ? competitorCost(price, users, numbers) : null;
  const partial = price ? missesNumberPrice(price, users, numbers) : false;
  const saving = theirs !== null && r.monthly !== null ? theirs - r.monthly : null;

  return (
    <SitePage title={`Ringlite vs ${c.name}`} description={c.summary}>
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />COMPARE</p>
        <h1 className="ms-h1">Ringlite vs {c.name}</h1>
        <p className="ms-lede">{c.summary}</p>
        <div className="ms-cta-row">
          <Link className="rl-button" to="/signbox">
            Get started
            <ArrowUpRight size={16} aria-hidden="true" />
          </Link>
          <Link className="ms-ghost" to="/sales">Talk to sales</Link>
        </div>
      </section>

      <section className="ms-band ms-band-dark">
        <div className="ms-costcard rl-wrap rl-reveal">
          <h2>The same team on both</h2>
          <div className="ms-stepper-row">
            <Stepper label="People" value={users} min={1} max={50} onChange={setUsers} />
            <Stepper label="Phone numbers" value={numbers} min={1} max={40} onChange={setNumbers} />
          </div>
          <div className="ms-cost-grid">
            <div className="ms-cost-line is-us">
              <span className="rl-mono">Ringlite</span>
              <strong>{r.monthly !== null ? `${money(r.monthly)}/mo` : "Custom"}</strong>
              <small>{r.plan.name} plan{r.monthly !== null ? " + usage past the minute pool" : ""}</small>
            </div>
            <div className="ms-cost-line">
              <span className="rl-mono">{c.name}</span>
              <strong>{theirs !== null ? `${money(Math.round(theirs))}/mo${partial ? "*" : ""}` : "Prices through sales"}</strong>
              <small>{price ? `${competitorTier(price, users).name}, ${Math.max(users, price.minSeats)} seats` : "Not published"}</small>
            </div>
          </div>
          {saving !== null && saving > 0 && (
            <p className="ms-saving">You keep {money(Math.round(saving))} a month, {money(Math.round(saving * 12))} a year.</p>
          )}
          <p className="ms-footnote">
            Monthly list prices, {COMPETITORS_CHECKED}: seats with the vendor's minimum, plus numbers
            beyond one per seat.{partial ? ` * ${c.name} does not publish its extra-number price, so extra numbers are left out.` : ""}{" "}
            Ringlite includes an exact shared pool of call minutes ({minutePoolsLine()} a month), then
            published per-minute and per-text rates.
          </p>
        </div>
      </section>

      <section className="rl-wrap rl-reveal">
        <div className="ms-table-wrap">
          <table className="ms-table">
            <thead>
              <tr>
                <th scope="col" />
                <th scope="col">{c.name}</th>
                <th scope="col">Ringlite</th>
              </tr>
            </thead>
            <tbody>
              {c.rows.map(row => (
                <tr key={row.label}>
                  <th scope="row">{row.label}</th>
                  <td>{row.them}</td>
                  <td>{row.us}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="ms-honest-grid rl-wrap rl-reveal">
        <div className="ms-honest is-us">
          <h2>Where Ringlite is stronger</h2>
          <ul>
            {c.weWin.map(item => (
              <li key={item}><Check size={17} aria-hidden="true" />{item}</li>
            ))}
          </ul>
        </div>
        <div className="ms-honest">
          <h2>Where {c.name} is stronger</h2>
          <ul>
            {c.theyWin.map(item => (
              <li key={item}><Plus size={17} aria-hidden="true" />{item}</li>
            ))}
          </ul>
        </div>
      </section>

      <section className="ms-steps rl-wrap rl-reveal">
        <h2>Switching is simple</h2>
        <ol>
          <li><span className="ms-step-n">1</span>Sign up and get verified</li>
          <li><span className="ms-step-n">2</span><Link to="/product/numbers">Port your numbers</Link></li>
          <li><span className="ms-step-n">3</span>Keep your old service until the port completes</li>
        </ol>
      </section>

      <section className="ms-faq-section rl-wrap rl-reveal">
        <h2>Switching questions</h2>
        <FaqList ids={["migrate", "port-in", "port-time", "per-number"]} />
      </section>

      <CtaBand title="Bring your number. Bring your team." />
    </SitePage>
  );
}
