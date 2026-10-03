/**
 * /switch — how a team moves to Ringlite: signup and verification, new numbers the same day,
 * porting the existing numbers, and texting registration. Plain steps, no invented promises.
 */
import { Link } from "react-router-dom";
import { CtaBand, FaqList, SitePage } from "@/marketing/SiteChrome";
import { ALTERNATIVE_SLUGS, COMPETITORS } from "@/marketing/content";

const STEPS = [
  {
    title: "Sign up and get verified",
    body: "Enter your email and confirm your identity with Didit. Approval usually takes under an hour.",
  },
  {
    title: "Pick numbers and invite your team",
    body: "Choose local numbers by area code and start calling the same day.",
  },
  {
    title: "Port your existing numbers",
    body: "Porting is included on Team and Business and usually takes one to two weeks. Your account shows the status, and your old service keeps working until it completes.",
  },
  {
    title: "Turn on registered texting",
    body: "Fill in the 10DLC brand and campaign forms inside Ringlite. Carriers approve before your texts start sending.",
  },
];

export function SwitchPage() {
  const alternatives = COMPETITORS.filter(c => (ALTERNATIVE_SLUGS as readonly string[]).includes(c.slug));

  return (
    <SitePage title="Switch business phone providers without missing calls" description="Move your team to Ringlite: start on a new number the day you're approved, then port your existing numbers in one to two weeks.">
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />SWITCHING</p>
        <h1 className="ms-h1">Switch to Ringlite without missing a call</h1>
        <p className="ms-lede">
          Start on a new Ringlite number the day you are approved, and keep your current provider
          running until your numbers port over. Calls then follow the same people and the same
          conversations.
        </p>
      </section>

      <section className="rl-process">
        <div className="rl-wrap rl-process-grid">
          <div>
            <h2>How switching works</h2>
            <p>Four steps, and your old line keeps answering until the last one is done.</p>
          </div>
          <ol>
            {STEPS.map((step, i) => (
              <li key={step.title}>
                <span className="rl-step-number">{String(i + 1).padStart(2, "0")}</span>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="ms-textblock rl-wrap rl-reveal" aria-labelledby="from-h">
        <h2 id="from-h">Switching from</h2>
        <ul className="ms-textblock-list">
          {alternatives.map(c => (
            <li key={c.slug}>
              <Link className="rl-text-link" to={`/alternatives/${c.slug}`}>{c.name}</Link>
            </li>
          ))}
          <li><Link className="rl-text-link" to="/calculator">See what your team would pay</Link></li>
        </ul>
      </section>

      <section className="ms-faq-section rl-wrap rl-reveal" aria-labelledby="switch-faq-h">
        <h2 id="switch-faq-h">Switching questions</h2>
        <FaqList ids={["migrate", "port-in", "port-time", "local-numbers", "toll-free", "trial", "cancel"]} />
      </section>

      <CtaBand title="Start on Ringlite today. Bring your number when it's ready." />
    </SitePage>
  );
}
