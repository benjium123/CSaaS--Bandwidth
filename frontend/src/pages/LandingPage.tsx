import * as React from "react";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowRight, ArrowUpRight, Check, CheckCheck, ChevronDown, Headphones, MessageSquare, Mic, Phone, PhoneCall, Plus, Search } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import "@fontsource-variable/archivo";
import "./landing.css";
import { SiteHeader, SiteFooter } from "@/marketing/SiteChrome";
import { ChatWidget } from "@/marketing/ChatWidget";
import { PROOF_POINTS, SOLUTIONS } from "@/marketing/content";
import { MINUTES_PER_USER, PLANS, RATES, addOnLine, cents, money, packageLine } from "@/marketing/pricing.config";
import { faqById, type Faq } from "@/marketing/faq";

type PreviewMode = "Calls" | "Messages" | "Team notes";
const WAVE = [12, 23, 17, 34, 45, 24, 58, 38, 66, 47, 29, 53, 74, 40, 60, 31, 49, 68, 35, 54, 25, 42, 62, 33, 49, 19, 31, 15];
const PEOPLE = [{ name: "Jamie Parker", initials: "JP", text: "Perfect. See you Thursday!", time: "Now" }, { name: "Morgan Ellis", initials: "ME", text: "Incoming call · 4 min", time: "12m" }, { name: "Alex Rivera", initials: "AR", text: "Thanks for the update.", time: "28m" }];

function Mark() {
  return <span className="rl-mark" aria-hidden="true"><span /><span /><span /></span>;
}

function ProductPreview() {
  const [mode, setMode] = React.useState<PreviewMode>("Calls");
  const [person, setPerson] = React.useState(0);
  const [playing, setPlaying] = React.useState(false);
  const selected = PEOPLE[person];
  return <div className="rl-product" id="product-preview">
    <div className="rl-product-bar"><span><Mark /> Ringlite <span className="rl-demo-label">INTERACTIVE PREVIEW</span></span><span className="rl-product-status"><i /> Your workspace</span></div>
    <div className="rl-product-body">
      <aside className="rl-mini-nav" aria-label="Preview inboxes"><span className="rl-mono">YOUR LINES</span><div className="rl-line-active"><Phone size={15} /><span>Main line<small>+1 (512) 555-0142</small></span></div><div><MessageSquare size={15} /><span>Conversations</span></div><div><Headphones size={15} /><span>Call history</span></div><div className="rl-mini-bottom"><span className="rl-avatar">Y</span> Your team</div></aside>
      <div className="rl-conversation-list"><div className="rl-list-title">Inbox <span>3</span><Search size={16} /></div><div className="rl-list-filter">All conversations <ChevronDown size={13} /></div>{PEOPLE.map((p, i) => <button key={p.name} onClick={() => setPerson(i)} className={i === person ? "is-selected" : ""} aria-pressed={i === person}><span className="rl-avatar">{p.initials}</span><span><strong>{p.name}</strong><small>{p.text}</small></span><time>{p.time}</time></button>)}<p className="rl-list-caption">A little context.<br />A much better conversation.</p></div>
      <div className="rl-thread"><div className="rl-thread-top"><span className="rl-avatar">{selected.initials}</span><span><strong>{selected.name}</strong><small>Customer · Main line</small></span><Phone size={17} /></div>
        <div className="rl-preview-tabs" role="group" aria-label="Explore the product preview">{(["Calls", "Messages", "Team notes"] as PreviewMode[]).map(tab => <button key={tab} aria-pressed={mode === tab} onClick={() => { setMode(tab); setPlaying(false); }}>{tab}</button>)}</div>
        <div className="rl-thread-content" aria-live="polite">
          {mode === "Calls" ? <div className="rl-call-demo"><span className="rl-live-label"><i /> A conversation worth keeping</span><div className="rl-call-avatar">{selected.initials}</div><h3>{selected.name}</h3><p>Thursday’s looking good.</p><div className={`rl-wave ${playing ? "is-playing" : ""}`} aria-hidden="true">{WAVE.map((h, i) => <span key={i} style={{ height: h, animationDelay: `${i * 38}ms` }} />)}</div><button className="rl-demo-play" onClick={() => setPlaying(!playing)} aria-pressed={playing}><PhoneCall size={16} /> {playing ? "Pause call preview" : "Try the call preview"}</button><small>Visual demo · no call is placed</small></div> : mode === "Messages" ? <div className="rl-message-demo"><span className="rl-date">TODAY · 10:42 AM</span><div className="rl-bubble incoming">Hi! Can we move our appointment to Thursday?</div><div className="rl-bubble outgoing">Absolutely. Does 2:30 work for you?</div><div className="rl-delivered"><CheckCheck size={13} /> Delivered</div><div className="rl-bubble incoming">Perfect. See you Thursday!</div><div className="rl-message-hint"><Check size={14} /> Texting after messaging registration approval</div></div> : <div className="rl-note-demo"><span className="rl-date">KEEP EVERYONE IN THE LOOP</span><div className="rl-note"><span>INTERNAL NOTE · ONLY YOUR TEAM</span><p><b>@Sam</b> Jamie’s appointment is now Thursday at 2:30. Everything you need is in this conversation.</p><small>Added by you · just now</small></div><div className="rl-note-follow"><span className="rl-avatar">S</span><p>Got it. I’ll take it from here.<small>Sam · Your team</small></p><CheckCheck size={16} /></div></div>}
        </div>
        <div className="rl-faux-composer"><span>{mode === "Team notes" ? "The next person starts with context." : "Every conversation, together."}</span><ArrowUpRight size={18} /></div>
      </div>
    </div>
  </div>;
}

const FAQ = ["personal-email", "signup-steps", "texting-how", "plans", "users", "per-number"]
  .map(id => faqById(id))
  .filter((f): f is Faq => !!f)
  .map(f => [f.q, f.a] as const);

export function LandingPage() {
  const { theme } = useSurfaceTheme();
  const { me } = useAuth();
  const root = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    const previous = document.title;
    document.title = "Ringlite — Your next great conversation starts here";
    const targets = root.current?.querySelectorAll(".rl-reveal");
    if (!("IntersectionObserver" in window)) return () => { document.title = previous; };
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add("is-visible"); observer.unobserve(entry.target); }
    }), { threshold: 0.08 });
    targets?.forEach(el => observer.observe(el));
    return () => { observer.disconnect(); document.title = previous; };
  }, []);
  const cta = me ? "/inbox" : "/signbox";
  return <div ref={root} className={`rl-landing console-surface ${surfaceThemeClass(theme)}`}>
    <a className="rl-skip" href="#main">Skip to content</a>
    <SiteHeader />
    <main id="main">
      <section className="rl-hero rl-wrap"><div className="rl-hero-copy"><p className="rl-eyebrow rl-enter"><span /> A BETTER LINE OF COMMUNICATION</p><h1 className="rl-enter">Small ring.<br />Big <span>possibilities.</span></h1><div className="rl-hero-bottom rl-enter"><p>Your business number. Your calls and texts.<br className="rl-desktop-break" /> One inbox that keeps the whole story together.</p><div className="rl-hero-links"><Link className="rl-button" to={cta}>Find your next connection <ArrowUpRight size={19} /></Link><a className="rl-text-link" href="#product">Take a look inside <ArrowDown size={16} /></a></div><span className="rl-price-teaser">From <b>$15</b> / number / month <span>+ usage</span></span></div></div>
        <div className="rl-orbit-scene rl-enter" aria-label="Illustration of a call and a follow-up conversation"><div className="rl-orbit rl-orbit-one" /><div className="rl-orbit rl-orbit-two" /><div className="rl-orbit rl-orbit-three" /><div className="rl-orbit-core"><Phone strokeWidth={1.3} /></div><span className="rl-orbit-label rl-mono">GOOD THINGS START WITH HELLO.</span><div className="rl-floating-call"><span className="rl-avatar">JP</span><div><strong>Jamie Parker</strong><small><i /> Incoming possibility</small></div><span className="rl-answer"><Phone size={20} /></span></div><div className="rl-floating-message"><MessageSquare size={18} /><p>“Let’s make it happen.”<span>The start of something good.</span></p><CheckCheck size={15} /></div><span className="rl-coordinate rl-mono">CALL. CONNECT. CONTINUE.</span></div>
      </section>
      <div className="rl-ticker" aria-label="Calling, texting and teamwork"><span>YOUR NUMBER.</span><Mark /><span>YOUR PEOPLE.</span><Mark /><span>YOUR NEXT CHAPTER.</span><Mark /></div>
      <div className="ms-proof rl-wrap" aria-label="Why Ringlite">{PROOF_POINTS.map(p => <span key={p}><i aria-hidden="true" />{p}</span>)}</div>
      <section id="product" className="rl-product-section rl-wrap rl-reveal"><div className="rl-section-heading"><p className="rl-eyebrow">01 / IN GOOD COMPANY</p><div><h2>A phone system.<br />With a longer memory.</h2><p>From the first ring to the next reply, keep the context close.<br />Explore a sample conversation below.</p></div></div><ProductPreview /><div className="rl-feature-strip"><div><Phone size={19} /><h3>Make it a conversation.</h3><p>Call from your browser with a dedicated number for your work.</p></div><div><MessageSquare size={19} /><h3>Pick up the thread.</h3><p>Keep calls and registered messaging together in one customer history.</p></div><div><Mic size={19} /><h3>Bring your team along.</h3><p>Share an inbox. Leave a note. Make the next handoff feel effortless.</p></div></div></section>
      <section id="how-it-works" className="rl-process rl-reveal"><div className="rl-wrap rl-process-grid"><div><p className="rl-eyebrow">02 / YOUR FIRST HELLO</p><h2>Start with you.<br />Grow from there.</h2><p>One signup, whether you work for yourself or with a team.</p><Link className="rl-text-link" to={cta}>Let’s get you connected <ArrowUpRight size={18} /></Link><div className="rl-process-art" aria-hidden="true"><span>you</span><div /><Mark /><div /><span>what’s next</span></div></div><ol>{[["01", "Make yourself at home.", "Sign up with a personal or work email, confirm it, and secure your account."], ["02", "A real person. A trusted line.", "Verify your identity with Didit and submit your application. Reviews typically take under one hour."], ["03", "Choose a number. Make a call.", "Once approved, find available numbers by area code, complete payment, and head to your inbox."]].map(([n, title, body]) => <li key={n}><span className="rl-step-number">{n}</span><div><h3>{title}</h3><p>{body}</p></div></li>)}<li className="rl-sms-step"><MessageSquare size={21} /><div><h3>Ready to text, too?</h3><p>Register your company and 10DLC campaign inside Ringlite. Carrier approval and number assignment unlock local-number messaging.</p></div></li></ol></div></section>
      <section id="pricing" className="rl-wrap rl-reveal ms-home-plans"><div className="rl-section-heading"><p className="rl-eyebrow">03 / ROOM TO GROW</p><div><h2>Your team and your numbers.<br />One simple price.</h2><p>Every plan includes users, phone numbers and {MINUTES_PER_USER} call minutes per user, shared. After that, calls are {cents(RATES.minute)} a minute and texts {cents(RATES.text)}.</p></div></div><div className="ms-plans ms-plans-4">{PLANS.map(plan => <article key={plan.code} className={plan.highlight ? "ms-plan is-highlight" : "ms-plan"}><div className="ms-plan-tag"><span className="rl-mono">{plan.name.toUpperCase()}</span>{plan.highlight ? <b className="rl-mono">MOST POPULAR</b> : null}</div><p className="ms-plan-tagline">{plan.tagline}</p><div className="ms-price">{plan.price !== null ? <><strong>{money(plan.price)}</strong><span>per month</span></> : <strong className="ms-price-custom">Let's talk</strong>}</div><ul className="ms-includes"><li>{packageLine(plan)}</li>{addOnLine(plan) ? <li>{addOnLine(plan)}</li> : null}</ul><Link className={plan.highlight ? "rl-button" : "ms-ghost"} to={plan.cta.to}>{plan.cta.label} <ArrowUpRight size={17} /></Link></article>)}</div><div className="ms-cta-row"><Link className="rl-text-link" to="/pricing">Compare plans and see the rate card <ArrowRight size={17} /></Link></div></section>
      <section className="rl-wrap rl-reveal ms-home-industries" aria-labelledby="industries-h"><p className="rl-eyebrow">BUILT FOR YOUR LINE OF WORK</p><h2 id="industries-h">Pick your world.</h2><div className="ms-chips">{SOLUTIONS.map(s => <Link key={s.slug} className="ms-chip" to={`/solutions/${s.slug}`}>{s.menu}</Link>)}</div></section>
      <section className="rl-faq rl-wrap rl-reveal" id="questions"><div><p className="rl-eyebrow">A FEW THINGS, ANSWERED</p><h2>Before we<br />say hello.</h2></div><div>{FAQ.map(([question, answer]) => <details key={question}><summary>{question}<Plus size={19} /></summary><p>{answer}</p></details>)}</div></section>
      <section className="rl-final rl-reveal"><div className="rl-wrap"><p className="rl-eyebrow">THE NEXT CONVERSATION IS YOURS.</p><h2>Make room<br />for <span>hello.</span><ArrowUpRight aria-hidden="true" /></h2><Link className="rl-button" to={cta}>Get started with Ringlite <ArrowUpRight size={20} /></Link><p>Your number. Your inbox. A new way to connect.</p></div></section>
    </main>
    <SiteFooter />
    <ChatWidget />
  </div>;
}
