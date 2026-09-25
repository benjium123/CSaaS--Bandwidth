/* Shared marketing chrome: header with mega panels, footer, icon map, FAQ list, CTA band.
   Every marketing page renders inside <SitePage> so the homepage theme variables apply. */
import * as React from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowUpRight, Bot, Briefcase, Building2, ChevronDown, Hash, Home, Inbox, KeyRound, ListChecks, Menu, MessageSquare, Moon, Phone, Plug, Plus, Printer, Scale, ShieldCheck, Sun, Users, Wrench, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { COMPETITORS, PRODUCTS, SOLUTIONS, TEAM_SOLUTIONS, type IconName } from "@/marketing/content";
import { COVERAGE, money, planByCode } from "@/marketing/pricing.config";
import { FAQS, faqById, faqsFor, type Faq, type FaqTopic } from "@/marketing/faq";
import { ChatWidget } from "@/marketing/ChatWidget";
import "@fontsource-variable/archivo";
import "@/pages/landing.css";
import "@/marketing/site.css";

const ICONS: Record<IconName, typeof Phone> = {
  phone: Phone, message: MessageSquare, inbox: Inbox, bot: Bot, list: ListChecks,
  printer: Printer, plug: Plug, hash: Hash, home: Home, key: KeyRound, wrench: Wrench,
  scale: Scale, shield: ShieldCheck, users: Users, briefcase: Briefcase, building: Building2,
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const Glyph = ICONS[name];
  return <Glyph size={size} aria-hidden="true" />;
}

function Mark() {
  return <span className="rl-mark" aria-hidden="true"><span /><span /><span /></span>;
}

const TEAM_YEARLY = planByCode("team").pricePerNumber.yearly;

const RESOURCES: { to: string; label: string; hint: string }[] = [
  { to: "/faq", label: "FAQ", hint: "The questions we get most" },
  { to: "/trust", label: "Security", hint: "Verification, isolation, fraud controls" },
  { to: "/legal/911", label: "911 disclosure", hint: "How emergency calling works" },
  { to: "/report", label: "Report a number", hint: "Tell us about an unexpected call" },
  { to: "/sales", label: "Talk to sales", hint: "Bigger teams and switching" },
];

interface PanelProps { onGo: () => void }

function ProductPanel({ onGo }: PanelProps) {
  return <div className="ms-mega-grid">
    <div className="ms-mega-col">
      {PRODUCTS.map(p => <Link className="ms-mega-item" key={p.slug} to={`/product/${p.slug}`} onClick={onGo}>
        <span className="ms-mega-icon"><Icon name={p.icon} /></span>
        <span><strong>{p.menu}</strong><small>{p.menuHint}</small></span>
      </Link>)}
    </div>
    <aside className="ms-mega-side">
      <div className="ms-mega-card">
        <p className="rl-mono">TEAM · YEARLY</p>
        <h3>Pay per line, not per person</h3>
        <p>{money(TEAM_YEARLY)} per number a month, billed yearly. Your team is included on the plan.</p>
        <Link className="rl-text-link" to="/pricing" onClick={onGo}>See pricing <ArrowUpRight size={15} aria-hidden="true" /></Link>
      </div>
    </aside>
  </div>;
}

function SolutionsPanel({ onGo }: PanelProps) {
  return <div className="ms-mega-grid">
    <div className="ms-mega-col">
      <p className="rl-mono">BY INDUSTRY</p>
      {SOLUTIONS.map(s => <Link className="ms-mega-item" key={s.slug} to={`/solutions/${s.slug}`} onClick={onGo}>
        <span className="ms-mega-icon"><Icon name={s.icon} /></span>
        <span><strong>{s.menu}</strong><small>{s.menuHint}</small></span>
      </Link>)}
    </div>
    <div className="ms-mega-col">
      <p className="rl-mono">BY TEAM</p>
      {TEAM_SOLUTIONS.map(t => <Link className="ms-mega-item" key={t.slug} to="/pricing" onClick={onGo}>
        <span className="ms-mega-icon"><Icon name={t.icon} /></span>
        <span><strong>{t.menu}</strong><small>{t.menuHint}</small></span>
      </Link>)}
    </div>
    <aside className="ms-mega-side">
      <div className="ms-mega-card">
        <p className="rl-mono">SWITCHING?</p>
        <h3>Compare before you move.</h3>
        <ul>{COMPETITORS.map(c => <li key={c.slug}><Link to={`/compare/${c.slug}`} onClick={onGo}>{c.name}</Link></li>)}</ul>
      </div>
    </aside>
  </div>;
}

function ResourcesPanel({ onGo }: PanelProps) {
  return <div className="ms-mega-grid">
    <div className="ms-mega-col">
      {RESOURCES.map(r => <Link className="ms-mega-item" key={r.to} to={r.to} onClick={onGo}>
        <span><strong>{r.label}</strong><small>{r.hint}</small></span>
      </Link>)}
    </div>
  </div>;
}

type PanelId = "product" | "solutions" | "resources";
const NAV: { id: PanelId; label: string }[] = [
  { id: "product", label: "Product" },
  { id: "solutions", label: "Solutions" },
  { id: "resources", label: "Resources" },
];

export function SiteHeader() {
  const { theme, toggle } = useSurfaceTheme();
  const { me } = useAuth();
  const { pathname } = useLocation();
  const [panel, setPanel] = React.useState<PanelId | null>(null);
  const [drawer, setDrawer] = React.useState(false);
  const [scrolled, setScrolled] = React.useState(false);
  const header = React.useRef<HTMLElement>(null);

  React.useEffect(() => { setPanel(null); setDrawer(false); }, [pathname]);

  React.useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  React.useEffect(() => {
    if (!panel && !drawer) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setPanel(null); setDrawer(false); }
    };
    const onPointer = (event: MouseEvent) => {
      if (header.current && !header.current.contains(event.target as Node)) { setPanel(null); setDrawer(false); }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [panel, drawer]);

  React.useEffect(() => {
    if (!drawer) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [drawer]);

  const close = () => { setPanel(null); setDrawer(false); };
  const themeLabel = `Switch to ${theme === "light" ? "dark" : "light"} theme`;
  const themeToggle = <button type="button" className="rl-theme" onClick={toggle} aria-label={themeLabel}>{theme === "light" ? <Moon size={18} aria-hidden="true" /> : <Sun size={18} aria-hidden="true" />}</button>;
  const ctaTo = me ? "/inbox" : "/signbox";

  return <header ref={header} className={scrolled ? "ms-header is-scrolled" : "ms-header"}>
    <div className="ms-header-inner">
      <Link className="rl-logo" to="/" onClick={close}><Mark />ringlite</Link>
      <nav className="ms-nav" aria-label="Main navigation">
        {NAV.map(item => <div className="ms-nav-item" key={item.id}>
          <button type="button" aria-expanded={panel === item.id} aria-controls={`ms-mega-${item.id}`} onClick={() => setPanel(panel === item.id ? null : item.id)}>
            {item.label}<ChevronDown size={14} aria-hidden="true" />
          </button>
        </div>)}
        <div className="ms-nav-item"><Link to="/pricing" onClick={close}>Pricing</Link></div>
      </nav>
      <div className="ms-actions">
        {themeToggle}
        <Link className="rl-login" to={me ? "/inbox" : "/login"} onClick={close}>{me ? "Open inbox" : "Log in"}</Link>
        <Link className="ms-ghost" to="/sales" onClick={close}>Talk to sales</Link>
        <Link className="rl-button rl-small" to={ctaTo} onClick={close}>{me ? "Workspace" : "Get started"}<ArrowUpRight size={16} aria-hidden="true" /></Link>
        <button type="button" className="rl-menu" aria-expanded={drawer} aria-controls="ms-drawer" aria-label={drawer ? "Close navigation" : "Open navigation"} onClick={() => { setPanel(null); setDrawer(!drawer); }}>{drawer ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}</button>
      </div>
      {panel && <div className="ms-mega" id={`ms-mega-${panel}`}>
        {panel === "product" ? <ProductPanel onGo={close} /> : panel === "solutions" ? <SolutionsPanel onGo={close} /> : <ResourcesPanel onGo={close} />}
      </div>}
    </div>
    {drawer && <div className="ms-drawer" id="ms-drawer">
      <details className="ms-drawer-group">
        <summary>Product</summary>
        {PRODUCTS.map(p => <Link key={p.slug} to={`/product/${p.slug}`} onClick={close}>{p.menu}</Link>)}
      </details>
      <details className="ms-drawer-group">
        <summary>Solutions</summary>
        {SOLUTIONS.map(s => <Link key={s.slug} to={`/solutions/${s.slug}`} onClick={close}>{s.menu}</Link>)}
        {TEAM_SOLUTIONS.map(t => <Link key={t.slug} to="/pricing" onClick={close}>{t.menu}</Link>)}
      </details>
      <details className="ms-drawer-group">
        <summary>Resources</summary>
        {RESOURCES.map(r => <Link key={r.to} to={r.to} onClick={close}>{r.label}</Link>)}
      </details>
      <Link to="/pricing" onClick={close}>Pricing</Link>
      <div className="ms-actions">
        {themeToggle}
        <Link className="rl-login" to={me ? "/inbox" : "/login"} onClick={close}>{me ? "Open inbox" : "Log in"}</Link>
        <Link className="ms-ghost" to="/sales" onClick={close}>Talk to sales</Link>
        <Link className="rl-button rl-small" to={ctaTo} onClick={close}>{me ? "Workspace" : "Get started"}<ArrowUpRight size={16} aria-hidden="true" /></Link>
      </div>
    </div>}
  </header>;
}

export function SiteFooter() {
  return <footer className="ms-footer">
    <div className="rl-wrap">
      <Link className="rl-logo" to="/"><Mark />ringlite</Link>
      <p>Good conversations start here.</p>
      <div className="ms-footer-cols">
        <div>
          <h2>Product</h2>
          <ul>{PRODUCTS.map(p => <li key={p.slug}><Link to={`/product/${p.slug}`}>{p.menu}</Link></li>)}</ul>
        </div>
        <div>
          <h2>Solutions</h2>
          <ul>{SOLUTIONS.map(s => <li key={s.slug}><Link to={`/solutions/${s.slug}`}>{s.menu}</Link></li>)}</ul>
        </div>
        <div>
          <h2>Company</h2>
          <ul>
            <li><Link to="/pricing">Pricing</Link></li>
            <li><Link to="/sales">Talk to sales</Link></li>
            <li><Link to="/faq">FAQ</Link></li>
            <li><Link to="/trust">Security</Link></li>
            <li><Link to="/login">Log in</Link></li>
          </ul>
        </div>
        <div>
          <h2>Legal</h2>
          <ul>
            <li><Link to="/legal/911">911 disclosure</Link></li>
            <li><Link to="/report">Report abuse</Link></li>
          </ul>
        </div>
      </div>
      <div className="ms-footer-bottom">
        <small>© {new Date().getFullYear()} Ringlite</small>
        <small>{COVERAGE}</small>
      </div>
    </div>
  </footer>;
}

export function FaqList({ ids, topic }: { ids?: string[]; topic?: FaqTopic }) {
  let items: Faq[];
  if (ids) items = ids.map(id => faqById(id)).filter((faq): faq is Faq => Boolean(faq));
  else if (topic) items = faqsFor(topic);
  else items = FAQS;
  return <div className="ms-faq-list">
    {items.map(faq => <details key={faq.id}>
      <summary>{faq.q}<Plus size={18} aria-hidden="true" /></summary>
      <p>{faq.a}</p>
    </details>)}
  </div>;
}

export function CtaBand({ title, body }: { title: string; body?: string }) {
  return <section className="rl-final rl-reveal">
    <div className="rl-wrap">
      <p className="rl-eyebrow">THE NEXT CONVERSATION IS YOURS.</p>
      <h2>{title}</h2>
      {body ? <p>{body}</p> : null}
      <div className="ms-cta-row">
        <Link className="rl-button" to="/signbox">Get started <ArrowUpRight size={19} aria-hidden="true" /></Link>
        <Link className="ms-ghost" to="/sales">Talk to sales</Link>
      </div>
    </div>
  </section>;
}

export function SitePage({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  const { theme } = useSurfaceTheme();
  const { pathname } = useLocation();
  const root = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const previous = document.title;
    document.title = `${title} · Ringlite`;
    const existing = document.querySelector<HTMLMetaElement>('meta[name="description"]');
    const previousDescription = existing?.content ?? "";
    const created = !existing;
    const tag = existing ?? document.createElement("meta");
    if (created) {
      tag.setAttribute("name", "description");
      document.head.appendChild(tag);
    }
    tag.setAttribute("content", description);
    return () => {
      document.title = previous;
      if (created) tag.remove();
      else tag.setAttribute("content", previousDescription);
    };
  }, [title, description]);

  React.useEffect(() => {
    window.scrollTo(0, 0);
    const targets = root.current?.querySelectorAll(".rl-reveal");
    if (!targets) return;
    if (!("IntersectionObserver" in window)) {
      targets.forEach(el => el.classList.add("is-visible"));
      return;
    }
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    }), { threshold: 0.08 });
    targets.forEach(el => observer.observe(el));
    return () => observer.disconnect();
  }, [pathname]);

  return <div ref={root} className={`rl-landing console-surface ${surfaceThemeClass(theme)} ms-site`}>
    <a className="rl-skip" href="#main">Skip to content</a>
    <SiteHeader />
    <main id="main">{children}</main>
    <SiteFooter />
    <ChatWidget />
  </div>;
}
