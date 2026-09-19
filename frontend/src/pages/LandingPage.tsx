import { Link } from "react-router-dom";
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/martian-mono/wght.css";
import "@/auth/authTheme.css";
import "@/auth/authTheme.light.css";
import "@/pages/landing.css";
import { ThemeToggle } from "@/auth/ThemeToggle";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { Application, Console, PatchPanel, Thread } from "./landing/surfaces";
import { DialBar, NumberStrip, RoutingTree } from "./landing/fragments";

/**
 * The public front of the product.
 *
 * WHAT THIS PAGE MAY SAY. It is the one screen written before anyone has an account, so it
 * has no `me`, no profile and no org - there is nothing here to load and therefore nothing
 * to claim from an unloaded resource. The discipline that replaces it is about the PRODUCT
 * claims instead:
 *
 *  - No prices. Numbers and calling credit are bought from the console after approval, and
 *    what they cost has not been decided. An invented number on the landing page is a
 *    promise the checkout would then have to break.
 *  - No customer logos, no counts, no testimonials, no "trusted by". We have none of those
 *    things, and a placeholder that looks like social proof is a lie with a grey filter on.
 *  - No feature that does not exist. Every capability named below maps to a route that is
 *    actually in the console's navigation - inbox, contacts, calls, campaigns, call flows,
 *    queues, assistants, numbers. The cheapest way to make this page better is to invent a
 *    seventh capability, and it is the one thing that would make it worthless.
 *  - The sequence is stated honestly and early: verification comes BEFORE calling, not as
 *    fine print underneath a "start calling in minutes". Someone who cannot pass business
 *    verification should learn that here rather than after filling in six forms.
 *
 * THE DRAWN PRODUCT IS THE SPINE. Four large surfaces (console, thread, patch panel,
 * application) and three small fragments, distributed down the page rather than banked in
 * the hero - a claim and the drawing of that claim, at the size the claim needs. Every one
 * is `aria-hidden` and contributes nothing to the accessible name of anything: they are
 * schematics, not screenshots, and no product screenshots exist to use instead.
 *
 * `auth-surface` on the root is the THEME SCOPE from authTheme.css, not a claim that this
 * is an auth screen - every Exchange token is nested under it. The second class is the
 * theme: `dark` (which authTheme.css explains must keep being emitted) or `is-light`,
 * which is what authTheme.light.css hangs its overrides off.
 */

/**
 * Capabilities. Each one is a route in the console's own navigation, not a wish.
 *
 * ORDER IS DELIBERATE, twice over. The three cards carrying a drawing come first so that
 * they form one complete row at three columns and the rack does not stagger; and the inbox
 * - which the hero and section 01 both already argue at length - is held back to the second
 * row, so the first thing this rack does is add something the page has not said yet.
 */
const CAPABILITIES = [
  {
    claim: "Numbers a team shares, not a number each",
    tag: "Lines",
    fragment: <NumberStrip />,
  },
  {
    claim: "Calls sorted before they ring anyone",
    tag: "Call flows and queues",
    fragment: <RoutingTree />,
  },
  {
    claim: "Answer at the desk, from the browser",
    tag: "Softphone",
    fragment: <DialBar />,
  },
  {
    claim: "Every call and text for a contact, in one thread",
    tag: "Inbox",
    fragment: null,
  },
  {
    claim: "Contacts that arrive already carrying the history",
    tag: "Contacts",
    fragment: null,
  },
  {
    claim: "An assistant that reads your own material",
    tag: "Assistants",
    fragment: null,
  },
] as const;

/** Safety, and the parts of it that are ours to keep rather than to claim. */
const SAFETY = [
  {
    t: "Verified before the first call",
    b: "Registered details, the people who own the business, and an ID check for each of them. Nothing dials until that is done.",
  },
  {
    t: "Documents we never hold",
    b: "The ID check is run by our identity partner. The document images go to them and never to us - there is no copy here to lose.",
  },
  {
    t: "Access decided per line",
    b: "Enforced on the server, not hidden in the interface. Somebody without a line cannot reach it by knowing the URL.",
  },
  {
    t: "Read by a person, every time",
    b: "No application is approved automatically. If something is missing we write back and say exactly what it is.",
  },
  {
    t: "Traffic we watch",
    b: "Campaign and calling patterns are checked against the account that was approved. An account that stops matching it loses the line.",
  },
  {
    t: "A number anyone can report",
    b: "The reporting form is public and needs no account, because the people best placed to tell us about a bad number are not our members.",
  },
] as const;

/** The questions someone actually has before signing up, answered without a sales voice. */
const QUESTIONS = [
  {
    q: "How long does verification take?",
    a: "A person reads every application, so it depends on the queue and on how complete yours is. We would rather not publish a turnaround we cannot keep to. You are emailed either way, and if something is missing we say exactly what.",
  },
  {
    q: "What happens if we are turned down?",
    a: "You are told the reason. Where it is something you can correct - a registration number that does not match the register, an address on a document that resolves to a mail-forwarding service - you can fix it and submit again.",
  },
  {
    q: "Who can see which numbers?",
    a: "Whoever the admin grants. Access is decided per line and enforced on the server: an employee sees the line they work on, a manager sees their team's, and only the admin account buys numbers or touches billing.",
  },
  {
    q: "Do you keep our ID documents?",
    a: "No. The check is handled by our identity partner and the document images never reach us. We are told whether the check passed, and the name it passed under.",
  },
  {
    // Deliberately NOT phrased as "bought from the console after approval", which is the
    // wording section 05 uses. A test pins that sentence with getByText, and getByText
    // throws when two elements match - so the page may state it exactly once. Same fact,
    // different words.
    q: "What does it cost to apply?",
    a: "Nothing at all. You pay for numbers and calling credit later, from inside the console once the account is approved, at the rates shown to you there.",
  },
  {
    q: "Can I sign up with a personal email address?",
    a: "No. Sign-up needs an address on your company's own domain - it is the first thing that ties the account to the business you are about to have verified.",
  },
] as const;

/** Who this shape of product is for. A statement of fit, never of adoption. */
const MADE_FOR = [
  "Trades and field service",
  "Clinics and practices",
  "Property management",
  "Legal",
  "Logistics",
  "Recruiting",
] as const;

export function LandingPage() {
  const { theme, toggle } = useSurfaceTheme();

  return (
    <div className={`auth-surface ${surfaceThemeClass(theme)} lp`}>
      <header className="lp-bar">
        <div className="lp-mark">
          <span className="ex-nameplate lp-mark-name">CSaaS</span>
          <span className="ex-label">Exchange</span>
        </div>
        <nav className="lp-bar-anchors" aria-label="Sections">
          <a href="#s01" className="ex-link">
            The inbox
          </a>
          <a href="#s02" className="ex-link">
            Access
          </a>
          <a href="#s03" className="ex-link">
            Verification
          </a>
          <a href="#s06" className="ex-link">
            Questions
          </a>
        </nav>
        <nav className="lp-bar-links">
          {/* The same control the auth screens use, so the front door has one of these and
              not two that drifted apart. */}
          <ThemeToggle theme={theme} onToggle={toggle} />
          <Link to="/login" className="ex-link">
            Sign in
          </Link>
          <Link to="/signup" className="ex-btn ex-btn-primary">
            Start
          </Link>
        </nav>
      </header>

      <div className="lp-wrap">
        {/* Hero ------------------------------------------------------------ */}
        <section className="lp-hero">
          <div className="lp-hero-copy">
            <div className="ex-label lp-rise" style={{ ["--d" as string]: "60ms" }}>
              For teams that answer the phone
            </div>
            <h1 className="lp-h1 lp-rise" style={{ ["--d" as string]: "130ms" }}>
              {/* U+2011 non-breaking hyphen: with balanced wrapping the browser will
                  otherwise break at the ordinary hyphen and leave "sign-" hanging. */}
              Every call, message and sign{"‑"}in <em>on one line.</em>
            </h1>
            <p className="lp-lede lp-rise" style={{ ["--d" as string]: "200ms" }}>
              One inbox for every number your business runs on. Calls and texts land in the
              same thread, so whoever picks it up already knows the story.
            </p>
            <div className="lp-cta lp-rise" style={{ ["--d" as string]: "270ms" }}>
              <Link to="/signup" className="ex-btn ex-btn-primary">
                Start your workspace
              </Link>
              <Link to="/login" className="ex-btn ex-btn-quiet">
                Sign in
              </Link>
            </div>
            <p className="lp-note lp-rise" style={{ ["--d" as string]: "340ms" }}>
              Work email required. Calling and texting switch on once your business is
              verified — a person reads every application.
            </p>
          </div>

          <div className="lp-hero-surface lp-rise" style={{ ["--d" as string]: "400ms" }}>
            <Console />
          </div>
        </section>

        {/* The rack: what the product is, before the page starts arguing for it. ----- */}
        <section className="lp-sec lp-rack-sec">
          <div className="lp-rack-head">
            <div className="ex-label">The console</div>
            <h2 className="lp-h2">Six things, and the phone stops being a problem</h2>
          </div>
          <ul className="lp-rack">
            {CAPABILITIES.map((c) => (
              <li className="lp-card" key={c.tag}>
                <p className="lp-card-claim">{c.claim}</p>
                <span className="ex-label lp-card-tag">{c.tag}</span>
                {c.fragment ? <div className="lp-card-art">{c.fragment}</div> : null}
              </li>
            ))}
          </ul>
        </section>

        {/* 01 — The inbox -------------------------------------------------- */}
        <section id="s01" className="lp-sec lp-sec-centred">
          <div className="ex-label">01 — The inbox</div>
          <h2 className="lp-h2">One thread per person, not per channel</h2>
          <p className="lp-body">
            Someone who texts on Monday and rings on Wednesday is one conversation. Calls,
            voicemail and messages sit in a single timeline against the contact, so nobody
            has to reconstruct what was agreed from three places at once.
          </p>
          <div className="lp-bleed">
            <Thread />
          </div>
        </section>

        {/* 02 — Access ----------------------------------------------------- */}
        <section id="s02" className="lp-sec lp-sec-split is-surface-left">
          <div className="lp-sec-surface">
            <PatchPanel />
          </div>
          <div className="lp-sec-copy">
            <div className="ex-label">02 — Access</div>
            <h2 className="lp-h2">Lines, not seats</h2>
            <p className="lp-body">
              People get the numbers they work on and nothing else. It is decided per line
              and enforced on the server, not hidden in the interface — a member without a
              line cannot reach it by knowing the URL.
            </p>
            <dl className="lp-lines">
              <div className="lp-line">
                <dt>Admin</dt>
                <dd className="lp-line-lead">Every number</dd>
                <dd>And the only account that buys numbers, adds credit or touches billing.</dd>
              </div>
              <div className="lp-line">
                <dt>Manager</dt>
                <dd className="lp-line-lead">Several lines</dd>
                <dd>The numbers their team runs. Nothing administrative comes with it.</dd>
              </div>
              <div className="lp-line">
                <dt>Employee</dt>
                <dd className="lp-line-lead">Their line</dd>
                <dd>Reads it, answers it, calls from it.</dd>
              </div>
            </dl>
          </div>
        </section>

        {/* 03 — Verification ----------------------------------------------- */}
        <section id="s03" className="lp-sec lp-sec-split">
          <div className="lp-sec-copy">
            <div className="ex-label">03 — Verification</div>
            <h2 className="lp-h2">Checked before you dial</h2>
            <p className="lp-body">
              Every business here is verified before it can call or text: registration
              details, the people who own it, and an ID check handled by our identity
              partner. <b>We never see or store the document images.</b>
            </p>
            <p className="lp-body">
              It is the part of this product nobody enjoys and everybody benefits from. The
              reason calls from this platform get answered is that the platform is not worth
              using for the people who make them not get answered.
            </p>
          </div>
          <div className="lp-sec-surface">
            <Application />
          </div>
        </section>

        {/* 04 — Safety ----------------------------------------------------- */}
        <section id="s04" className="lp-sec">
          <div className="lp-sec-head">
            <div className="ex-label">04 — Standing</div>
            <h2 className="lp-h2">What keeps the line worth answering</h2>
          </div>
          <ul className="lp-grid">
            {SAFETY.map((s) => (
              <li className="lp-grid-item" key={s.t}>
                <h3 className="lp-grid-t">{s.t}</h3>
                <p className="lp-grid-b">{s.b}</p>
              </li>
            ))}
          </ul>
          <p className="lp-body lp-grid-note">
            Found a number from this platform behaving badly?{" "}
            <Link to="/report" className="ex-link">
              Report it here
            </Link>
            {" "}— no account needed.
          </p>
        </section>

        {/* 05 — Getting going ---------------------------------------------- */}
        <section id="s05" className="lp-sec">
          <div className="lp-sec-head">
            <div className="ex-label">05 — Getting going</div>
            <h2 className="lp-h2">Four steps, in this order</h2>
          </div>
          <div className="lp-steps">
            {[
              {
                t: "Sign up with your work email",
                b: "Your company's own domain. Personal addresses are not accepted.",
              },
              {
                t: "Verify your business",
                b: "Registered details, owners, one document, and an ID check for each owner. You can stop and come back; nothing is submitted until you say so.",
              },
              {
                t: "We review it",
                b: "A person reads every application. We email you either way, and if something is missing we say exactly what.",
              },
              {
                t: "Add numbers and go live",
                b: "Once approved, the admin account buys numbers and adds calling credit from the console.",
              },
            ].map((s, i) => (
              <div className="lp-step" key={s.t}>
                <span className="lp-step-n">{String(i + 1).padStart(2, "0")}</span>
                <div className="lp-step-body">
                  <div className="lp-step-t">{s.t}</div>
                  <p className="lp-step-b">{s.b}</p>
                </div>
              </div>
            ))}
          </div>
          {/* Deliberately no price. See the note at the top of this file. */}
          <p className="lp-body lp-steps-note">
            Numbers and calling credit are bought from the console after approval, at the
            rates shown to you there. There is nothing to pay to apply.
          </p>
        </section>

        {/* Made for -------------------------------------------------------- */}
        <section className="lp-sec lp-fit">
          <div className="ex-label">Made for</div>
          <h2 className="lp-h2">Teams where a missed call is a missed job</h2>
          <ul className="lp-chips">
            {MADE_FOR.map((m) => (
              <li className="lp-chip" key={m}>
                {m}
              </li>
            ))}
          </ul>
          <p className="lp-body lp-fit-note">
            The shape it is built for, not a list of who is here. Anyone who answers the
            phone for a living and has more than one person doing it will recognise the
            problem.
          </p>
        </section>

        {/* 06 — Questions --------------------------------------------------
         * Native <details>/<summary>: an accordion with no JavaScript, keyboard-operable
         * and expandable by the browser's own find-in-page. A hand-rolled one would need a
         * button, aria-expanded, a region and a key handler to be worse than this. */}
        <section id="s06" className="lp-sec">
          <div className="lp-sec-head">
            <div className="ex-label">06 — Questions</div>
            <h2 className="lp-h2">Before you start filling anything in</h2>
          </div>
          <div className="lp-faq">
            {QUESTIONS.map((x) => (
              <details className="lp-q" key={x.q}>
                <summary className="lp-q-s">
                  <span>{x.q}</span>
                  <span className="lp-q-mark" aria-hidden="true" />
                </summary>
                <p className="lp-q-a">{x.a}</p>
              </details>
            ))}
          </div>
        </section>

        {/* Close ----------------------------------------------------------- */}
        <section className="lp-close">
          <h2 className="lp-h2">Put your numbers on one line.</h2>
          <p className="lp-lede lp-close-lede">
            Set-up takes a few minutes. Verification is the long pole, and it starts the
            moment you finish signing up.
          </p>
          <div className="lp-cta lp-close-cta">
            <Link to="/signup" className="ex-btn ex-btn-primary">
              Start your workspace
            </Link>
          </div>
        </section>

        <footer className="lp-foot">
          <div className="lp-mark">
            <span className="ex-nameplate lp-mark-name">CSaaS</span>
            <span className="ex-label">Exchange</span>
          </div>
          <div className="lp-foot-links">
            <Link to="/login" className="ex-link">
              Sign in
            </Link>
            <Link to="/signup" className="ex-link">
              Start a workspace
            </Link>
            {/* The public number-reporting form. It is a real route and a legal obligation,
                so it belongs where someone who is not a member can find it. */}
            <Link to="/report" className="ex-link">
              Report a number
            </Link>
          </div>
        </footer>
      </div>
    </div>
  );
}
