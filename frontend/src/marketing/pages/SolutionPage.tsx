import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { CtaBand, FaqList, Icon, SitePage } from "@/marketing/SiteChrome";
import { PRODUCTS, SOLUTIONS } from "@/marketing/content";
import { addOnLine, money, packageLine, planByCode } from "@/marketing/pricing.config";

/** Products shown under "What you get", keyed by solution slug. */
const DEFAULT_PRODUCT_SLUGS: readonly string[] = ["calling", "inbox", "texting"];

const SOLUTION_PRODUCT_SLUGS: Record<string, readonly string[]> = {
  "real-estate": ["inbox", "texting", "dialer"],
  insurance: ["inbox", "texting", "dialer"],
  recruiting: ["inbox", "texting", "dialer"],
};

/** First letters of the first two words of a contact name, for the thread avatar. */
function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part.charAt(0).toUpperCase())
    .join("");
}

/** "Do this. Then this." -> { head: "Do this. ", tail: "Then this." } for the last sentence. */
function splitTrailingSentence(title: string): { head: string; tail: string } {
  const lastDot = title.lastIndexOf(". ");
  if (lastDot === -1) return { head: title, tail: "" };
  return { head: title.slice(0, lastDot + 2), tail: title.slice(lastDot + 2) };
}

/** Lead with the @mention (first word) of a team note, the rest as body copy. */
function splitMention(note: string): { mention: string; rest: string } {
  const parts = note.split(" ");
  return { mention: parts[0], rest: parts.slice(1).join(" ") };
}

export function SolutionPage() {
  const { slug } = useParams<{ slug: string }>();
  const solution = SOLUTIONS.find(item => item.slug === slug);
  if (!solution) return <Navigate to="/" replace />;

  const plan = planByCode(solution.plan);
  const { head, tail } = splitTrailingSentence(solution.title);
  const { mention, rest } = splitMention(solution.thread.note);

  const slugs = SOLUTION_PRODUCT_SLUGS[solution.slug] ?? DEFAULT_PRODUCT_SLUGS;
  const pointProducts = slugs.flatMap(productSlug => {
    const product = PRODUCTS.find(item => item.slug === productSlug);
    return product ? [product] : [];
  });

  const allowance = "Calls and texts are pay as you go at the published rates.";

  return (
    <SitePage title={solution.menu} description={solution.lede}>
      <section className="ms-page-hero ms-split rl-wrap">
        <div className="ms-hero-copy">
          <p className="rl-eyebrow">
            <span />
            {`SOLUTIONS / ${solution.menu.toUpperCase()}`}
          </p>
          <h1 className="ms-h1">
            {head}
            {tail ? <span>{tail}</span> : null}
          </h1>
          <p className="ms-lede">{solution.lede}</p>
          <div className="ms-cta-row">
            <Link className="rl-button" to="/signbox">Get a local number</Link>
            <Link className="ms-ghost" to={`/sales?industry=${solution.slug}`}>
              Talk to sales <ArrowUpRight size={16} />
            </Link>
          </div>
        </div>

        <div className="ms-thread" aria-hidden="true">
          <div className="ms-thread-head">
            <span className="rl-avatar">{initials(solution.thread.contact)}</span>
            <div>
              <p className="ms-thread-name">{solution.thread.contact}</p>
              <small>{solution.thread.context}</small>
            </div>
          </div>
          <div className="ms-callrow">
            <i className="ms-live" />
            {solution.thread.missed}
          </div>
          <div className="rl-bubble incoming">{solution.thread.incoming}</div>
          <div className="rl-bubble outgoing">{solution.thread.outgoing}</div>
          <div className="rl-note">
            <p>
              <b>{mention}</b> {rest}
            </p>
          </div>
        </div>
      </section>

      <section className="ms-pains rl-wrap rl-reveal">
        {solution.pains.map(pain => (
          <article className="ms-pain" key={pain.label}>
            <span className="rl-mono">{pain.label}</span>
            <h3>{pain.title}</h3>
            <p>{pain.body}</p>
          </article>
        ))}
      </section>

      <section className="ms-points rl-wrap rl-reveal">
        <h2>What you get</h2>
        <div className="ms-points-grid">
          {pointProducts.map(product => (
            <article className="ms-point" key={product.slug}>
              <Icon name={product.icon} size={20} />
              <h3>{product.menu}</h3>
              <p>{product.lede}</p>
              <Link className="rl-text-link" to={`/product/${product.slug}`}>
                Learn more
              </Link>
            </article>
          ))}
        </div>
      </section>

      <aside className="ms-planfit rl-wrap rl-reveal">
        <div className="ms-planfit-copy">
          <p className="rl-eyebrow">
            <span />
            PLAN FIT
          </p>
          <h2>{`${plan.name} fits most ${solution.menu.toLowerCase()} teams`}</h2>
          <p>{`${packageLine(plan)}. ${addOnLine(plan) ?? ""}`}</p>
          <p>{plan.price !== null ? `${money(plan.price)} a month.` : "Custom pricing."}</p>
          <p>{allowance}</p>
          <Link className="rl-text-link" to="/pricing">
            See pricing
          </Link>
        </div>
      </aside>

      <section className="rl-wrap rl-reveal ms-faq-section">
        <h2>{`Questions from ${solution.menu.toLowerCase()} teams`}</h2>
        <FaqList ids={solution.faqIds} />
      </section>

      <CtaBand title={`Ringlite for ${solution.menu.toLowerCase()}.`} />
    </SitePage>
  );
}
