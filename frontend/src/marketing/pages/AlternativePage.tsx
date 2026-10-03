/**
 * /alternatives/:slug — the switch-from page for each per-seat vendor: why teams leave, what the
 * same team pays on both, where the other vendor is the better pick, and how the move works.
 */
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import { ALTERNATIVE_SLUGS, COMPETITORS } from "@/marketing/content";
import {
  COMPETITORS_CHECKED,
  COMPETITOR_SEAT_PRICES,
  competitorCost,
  money,
  recommend,
} from "@/marketing/pricing.config";

const TABLE_SIZES = [3, 5, 10, 15];

/** "Quo (OpenPhone)" -> "Quo": the short name reads better in a sentence. */
function shortName(name: string): string {
  return name.replace(/\s*\([^)]*\)\s*$/, "");
}

export function AlternativePage() {
  const { slug } = useParams<{ slug: string }>();
  const c = COMPETITORS.find(entry => entry.slug === slug);
  const p = COMPETITOR_SEAT_PRICES.find(entry => entry.slug === slug);
  if (!slug || !(ALTERNATIVE_SLUGS as readonly string[]).includes(slug) || !c || !p) {
    return <Navigate to="/" replace />;
  }
  const short = shortName(c.name);

  return (
    <SitePage title={`${c.name} alternative for small teams`} description={`Looking for a ${short} alternative? ${c.summary}`}>
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />ALTERNATIVE</p>
        <h1 className="ms-h1">The {short} alternative priced for small teams</h1>
        <p className="ms-lede">{c.summary}</p>
        <div className="ms-cta-row">
          <Link className="rl-button" to="/signbox">
            Get started
            <ArrowUpRight size={16} aria-hidden="true" />
          </Link>
          <Link className="ms-ghost" to={`/compare/${c.slug}`}>See the full comparison</Link>
        </div>
      </section>

      <section className="ms-textblock rl-wrap rl-reveal" aria-labelledby="why-h">
        <h2 id="why-h">Why teams switch from {short}</h2>
        <ul className="ms-textblock-list">
          {c.weWin.map(item => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      <section className="ms-matrix-section rl-wrap" aria-labelledby="same-team-h">
        <h2 id="same-team-h">What the same team pays</h2>
        <table className="ms-matrix-table">
          <caption>Monthly list prices before usage, checked {COMPETITORS_CHECKED}.</caption>
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col">Ringlite</th>
              <th scope="col">{c.name}</th>
              <th scope="col">You keep, per month</th>
            </tr>
          </thead>
          <tbody>
            {TABLE_SIZES.map(n => {
              const r = recommend(n, n);
              const theirs = Math.round(competitorCost(p, n, n));
              const difference = r.monthly === null ? null : theirs - r.monthly;
              return (
                <tr key={n}>
                  <th scope="row">{n} people, {n} numbers</th>
                  <td>{r.plan.name} {r.monthly !== null ? money(r.monthly) : "Custom"}</td>
                  <td>{money(theirs)}</td>
                  <td>{difference !== null && difference > 0 ? money(difference) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="ms-textblock rl-wrap rl-reveal" aria-labelledby="they-win-h">
        <h2 id="they-win-h">Where {short} is the better pick</h2>
        <ul className="ms-textblock-list">
          {c.theyWin.map(item => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p className="ms-footnote">If those matter most, they may suit you better.</p>
      </section>

      <section className="ms-textblock rl-wrap rl-reveal" aria-labelledby="switching-h">
        <h2 id="switching-h">Switching takes one signup</h2>
        <p>
          Sign up with your email and confirm your identity with Didit. Approval usually takes under
          an hour, then you pick numbers by area code and start calling the same day on a new number.
        </p>
        <p>
          Porting your existing numbers is included on Team and Business and usually takes one to two
          weeks. Keep your old provider running until the port completes.
        </p>
        <p>
          <Link className="rl-text-link" to="/switch">How switching works</Link>
        </p>
      </section>

      <section className="ms-faq-section rl-wrap rl-reveal" aria-labelledby="alt-faq-h">
        <h2 id="alt-faq-h">Common questions</h2>
        <FaqList ids={[`vs-${c.slug}`, "migrate", "port-in", "port-time", "unlimited"]} />
      </section>

      <CtaBand title="Leave per-seat pricing behind." />
    </SitePage>
  );
}
