import { ArrowUpRight } from "lucide-react";
import { Link, Navigate, useParams } from "react-router-dom";

import { CtaBand, FaqList, Icon, SitePage } from "@/marketing/SiteChrome";
import { PRODUCTS } from "@/marketing/content";
import { money, planByCode } from "@/marketing/pricing.config";

/** The faux app window shown beside the hero copy, chosen by product slug. */
function visualBody(slug: string) {
  if (slug === "calling" || slug === "dialer") {
    return (
      <div className="ms-callcard">
        <span className="rl-avatar">JP</span>
        <div>
          <p>Jamie Parker</p>
          <p className="ms-muted">
            <i className="ms-live" /> On a call · 04:12
          </p>
        </div>
      </div>
    );
  }

  if (slug === "texting" || slug === "inbox" || slug === "ai") {
    return (
      <>
        <div className="rl-bubble incoming">Hi! Can we move our appointment to Thursday?</div>
        <div className="rl-bubble outgoing">Absolutely. Does 2:30 work for you?</div>
        <p className="ms-muted">Delivered</p>
      </>
    );
  }

  if (slug === "numbers") {
    return (
      <>
        <div className="ms-number-row">+1 (512) 555-0142 · Austin</div>
        <div className="ms-number-row">+1 (214) 555-0199 · Dallas</div>
        <div className="ms-number-row">+1 (888) 555-0110 · Toll-free</div>
      </>
    );
  }

  if (slug === "fax") {
    return <div className="ms-number-row">Signed lease.pdf · 3 pages · Delivered</div>;
  }

  return (
    <pre className="rl-mono ms-code">
      {'{"event":"call.completed","from":"+15125550142","duration":252}'}
    </pre>
  );
}

function ProductVisual({ slug }: { slug: string }) {
  return (
    <div className="ms-visual" aria-hidden="true">
      <div className="ms-visual-bar">
        <span className="rl-mark">
          <span />
          <span />
          <span />
        </span>
        <span>Ringlite</span>
      </div>
      {visualBody(slug)}
    </div>
  );
}

export function ProductPage() {
  const { slug } = useParams<{ slug: string }>();
  const product = PRODUCTS.find(item => item.slug === slug);

  if (!product) return <Navigate to="/" replace />;

  const plan = planByCode(product.plan);
  const index = PRODUCTS.findIndex(item => item.slug === product.slug);
  const related = PRODUCTS.slice(index + 1)
    .concat(PRODUCTS.slice(0, index))
    .slice(0, 3);

  return (
    <SitePage title={product.menu} description={product.lede}>
      <section className="ms-page-hero ms-split rl-wrap">
        <div>
          <p className="rl-eyebrow">
            <span /> {product.eyebrow}
          </p>
          <h1 className="ms-h1">{product.title}</h1>
          <p className="ms-lede">{product.lede}</p>
          <div className="ms-cta-row">
            <Link className="rl-button" to="/signbox">
              Get started <ArrowUpRight size={16} />
            </Link>
            <Link className="ms-ghost" to="/sales">
              Talk to sales
            </Link>
          </div>
        </div>
        <ProductVisual slug={product.slug} />
      </section>

      <section className="ms-points rl-wrap rl-reveal">
        {product.points.map(point => (
          <div key={point.title}>
            <Icon name={product.icon} size={22} />
            <h3>{point.title}</h3>
            <p>{point.body}</p>
          </div>
        ))}
      </section>

      <section className="ms-steps rl-wrap rl-reveal">
        <h2>How it works</h2>
        <ol>
          {product.steps.map((step, stepIndex) => (
            <li key={step}>
              <span className="ms-step-n">{stepIndex + 1}</span> {step}
            </li>
          ))}
        </ol>
      </section>

      <aside className="ms-planfit rl-wrap rl-reveal">
        <div>
          <p className="rl-mono">PLAN FIT</p>
          <h2>Included on {plan.name} and up</h2>
          <p>{money(plan.pricePerNumber.yearly)} per number / month, billed yearly.</p>
        </div>
        <Link className="rl-text-link" to="/pricing">
          See pricing
        </Link>
      </aside>

      <section className="ms-related rl-wrap rl-reveal">
        <h2>Explore more</h2>
        {related.map(item => (
          <Link key={item.slug} to={`/product/${item.slug}`}>
            <Icon name={item.icon} size={20} />
            <h3>{item.menu}</h3>
            <p>{item.menuHint}</p>
          </Link>
        ))}
      </section>

      <section className="rl-wrap rl-reveal ms-faq-section">
        <h2>Questions</h2>
        <FaqList ids={product.faqIds} />
      </section>

      <CtaBand title="Make it a conversation." />
    </SitePage>
  );
}
