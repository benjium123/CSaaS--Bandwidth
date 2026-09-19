import { Link } from "react-router-dom";
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/martian-mono/wght.css";
import "@/auth/authTheme.css";
import "@/pages/landing.css";

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
 *  - The sequence is stated honestly and early: verification comes BEFORE calling, not as
 *    fine print underneath a "start calling in minutes". Someone who cannot pass business
 *    verification should learn that here rather than after filling in six forms.
 *
 * `auth-surface` on the root is the THEME SCOPE from authTheme.css, not a claim that this
 * is an auth screen - every Exchange token is nested under it.
 */
export function LandingPage() {
  return (
    <div className="auth-surface dark lp">
      <header className="lp-bar">
        <div className="lp-mark">
          <span className="ex-nameplate text-[1.125rem] text-[hsl(var(--ex-bone))]">CSaaS</span>
          <span className="ex-label">Exchange</span>
        </div>
        <nav className="lp-bar-links">
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
          <div>
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

          {/* Decorative, and aria-hidden: it is a drawing of the product, not a source of
              information. Everything it implies is stated in real copy below. */}
          <div
            className="lp-panel lp-rise"
            aria-hidden="true"
            style={{ ["--d" as string]: "400ms" }}
          >
            <div className="lp-panel-head">
              <span className="ex-label">All conversations</span>
              <span className="ex-label">3 lines</span>
            </div>
            {[
              { n: "Dana Whitfield", p: "Missed call · 2m 14s voicemail", t: "09:12", on: true, s: "live" },
              { n: "Marcus Webb", p: "Can you do Thursday morning instead?", t: "08:47", on: false, s: "idle" },
              { n: "Priya Raman", p: "Text delivered · quote sent", t: "Yest", on: false, s: "wait" },
            ].map((r) => (
              <div key={r.n} className={"lp-row" + (r.on ? " is-on" : "")}>
                <span className={"ex-lamp ex-lamp-" + r.s} />
                <span className="min-w-0">
                  <span className="lp-who">{r.n}</span>
                  <span className="lp-prev">{r.p}</span>
                </span>
                <span className="lp-when">{r.t}</span>
              </div>
            ))}
          </div>
        </section>

        {/* One thread ------------------------------------------------------ */}
        <section className="lp-sec">
          <div className="lp-sec-grid">
            <div>
              <div className="ex-label">01 — The inbox</div>
              <h2 className="lp-h2">One thread per person, not per channel</h2>
            </div>
            <div>
              <p className="lp-body">
                A customer who texts on Monday and rings on Wednesday is one conversation.
                Calls, voicemail and messages sit in a single timeline against the contact,
                so nobody has to reconstruct what was agreed from three places at once.
              </p>
              <p className="lp-body">
                Mark a thread important, mark it unread for whoever comes on next, and get on
                with it. <b>Contact details open when you want them</b> and stay out of the
                way when you don't.
              </p>
            </div>
          </div>
        </section>

        {/* Lines ----------------------------------------------------------- */}
        <section className="lp-sec">
          <div className="lp-sec-grid">
            <div>
              <div className="ex-label">02 — Access</div>
              <h2 className="lp-h2">Lines, not seats</h2>
            </div>
            <div>
              <p className="lp-body">
                People get the numbers they work on and nothing else. It is decided per line
                and enforced on the server, not hidden in the interface — a member without a
                line cannot reach it by knowing the URL.
              </p>
              <dl className="lp-lines">
                <div className="lp-line">
                  <dt>Admin</dt>
                  <dd className="text-[hsl(var(--ex-bone))]">Every number</dd>
                  <dd>
                    And the only account that buys numbers, adds credit, sets the API key or
                    touches billing.
                  </dd>
                </div>
                <div className="lp-line">
                  <dt>Manager</dt>
                  <dd className="text-[hsl(var(--ex-bone))]">Several lines</dd>
                  <dd>The numbers their team runs. Nothing administrative comes with it.</dd>
                </div>
                <div className="lp-line">
                  <dt>Employee</dt>
                  <dd className="text-[hsl(var(--ex-bone))]">Their line</dd>
                  <dd>Reads it, answers it, calls from it.</dd>
                </div>
              </dl>
            </div>
          </div>
        </section>

        {/* Verification ---------------------------------------------------- */}
        <section className="lp-sec">
          <div className="lp-sec-grid">
            <div>
              <div className="ex-label">03 — Verification</div>
              <h2 className="lp-h2">Checked before you dial</h2>
            </div>
            <div>
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
          </div>
        </section>

        {/* How ------------------------------------------------------------- */}
        <section className="lp-sec">
          <div className="lp-sec-grid">
            <div>
              <div className="ex-label">04 — Getting going</div>
              <h2 className="lp-h2">Four steps, in this order</h2>
            </div>
            <div>
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
                    <div>
                      <div className="lp-step-t">{s.t}</div>
                      <p className="lp-step-b">{s.b}</p>
                    </div>
                  </div>
                ))}
              </div>
              {/* Deliberately no price. See the note at the top of this file. */}
              <p className="lp-body" style={{ marginTop: "1.5rem" }}>
                Numbers and calling credit are bought from the console after approval, at the
                rates shown to you there. There is nothing to pay to apply.
              </p>
            </div>
          </div>
        </section>

        {/* Close ----------------------------------------------------------- */}
        <section className="lp-close">
          <h2 className="lp-h2">Put your numbers on one line.</h2>
          <p className="lp-lede" style={{ margin: "1rem auto 0" }}>
            Set-up takes a few minutes. Verification is the long pole, and it starts the
            moment you finish signing up.
          </p>
          <div className="lp-cta">
            <Link to="/signup" className="ex-btn ex-btn-primary">
              Start your workspace
            </Link>
          </div>
        </section>

        <footer className="lp-foot">
          <div className="lp-mark">
            <span className="ex-nameplate text-[hsl(var(--ex-bone))]">CSaaS</span>
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
                so it belongs where someone who is not a customer can find it. */}
            <Link to="/report" className="ex-link">
              Report a number
            </Link>
          </div>
        </footer>
      </div>
    </div>
  );
}
