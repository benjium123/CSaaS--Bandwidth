import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { SitePage, FaqList, CtaBand } from "@/marketing/SiteChrome";
import { COMPETITORS, type Competitor } from "@/marketing/content";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  minutePoolsLine,
  money,
  recommend,
} from "@/marketing/pricing.config";

/** "Quo (OpenPhone)" -> "quo"; "CallHippo Professional" -> "callhippo". */
function firstWord(name: string): string {
  const [word = ""] = name.trim().split(/\s+/);
  return word.replace(/[()]/g, "").toLowerCase();
}

export function ComparePage() {
  const { slug } = useParams<{ slug: string }>();
  const competitor: Competitor | undefined = COMPETITORS.find(c => c.slug === slug);
  if (!competitor) return <Navigate to="/" replace />;
  const c = competitor;

  const r = recommend(5, 2);
  const seat = COMPETITOR_SEAT_PRICES.find(e => firstWord(e.name) === firstWord(c.name));

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

      <section className="ms-costcard rl-wrap rl-reveal">
        <h2>A team of 5 with 2 phone numbers</h2>
        <div className="ms-cost-grid">
          <p className="ms-cost-line">
            <span className="rl-mono">Ringlite</span>
            {money(r.monthly ?? 0)}/mo on {r.plan.name} + usage
          </p>
          {seat ? (
            <p className="ms-cost-line">
              <span className="rl-mono">{c.name}</span>
              {money(Math.max(5, seat.minSeats) * seat.monthly)}/mo on {seat.name}
            </p>
          ) : null}
        </div>
        <p className="ms-footnote">Monthly list prices, {COMPETITORS_CHECKED}. Ringlite includes an exact shared pool of call minutes ({minutePoolsLine()} a month), then published per-minute and per-text rates.</p>
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

      <section className="ms-honest rl-wrap rl-reveal">
        <h2>Where {c.name} is stronger</h2>
        <ul>
          {c.theyWin.map(item => (
            <li key={item}>{item}</li>
          ))}
        </ul>
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
