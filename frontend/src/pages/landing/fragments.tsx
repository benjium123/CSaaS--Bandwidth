/**
 * Small drawn product fragments, one per capability card.
 *
 * The page already carries four large drawings (the console, the thread, the patch panel,
 * the application). These are the opposite end of the same idea: a card claims one thing,
 * and the fragment beside it shows that one thing and nothing else. It is the pattern the
 * better competitors use - the product is not shown once at the top and then described for
 * the rest of the page, it keeps appearing at the size the claim needs.
 *
 * RULES, same as the big surfaces:
 *  - Every fragment is `aria-hidden`. They are schematics, not screenshots, and no fragment
 *    is the only place a claim is made - the card says it in prose first.
 *  - CSS and inline SVG only. The console's CSP is `img-src 'self' data: blob:` and
 *    `script-src 'self'`, so there is no asset to fetch and no library to reach for.
 *  - Numbers are masked with mid-dots. Not decoration: a real-looking phone number on a
 *    public page is a number somebody eventually dials.
 *  - No currency symbol may appear anywhere near a digit, here or in any card copy. There
 *    is a test asserting the whole page is free of `[$£€]\s?\d`, because no price has been
 *    decided and a number here is a promise the checkout would have to break.
 *  - Nothing is randomised. Fixed values at module scope, so the page draws identically
 *    on every render and every visit.
 */
import "./fragments.css";

/** Three lines on the account, as the numbers settings page lists them. */
export function NumberStrip() {
  const rows = [
    { cc: "US", n: "+1 415 ••• ••42", s: "live" },
    { cc: "UK", n: "+44 20 •••• ••18", s: "live" },
    { cc: "US", n: "+1 212 ••• ••07", s: "wait" },
  ] as const;
  return (
    <div className="lpf lpf-numbers" aria-hidden="true">
      {rows.map((r) => (
        <div className="lpf-row" key={r.n}>
          <span className={"ex-lamp ex-lamp-" + r.s} />
          <span className="ex-mono lpf-cc">{r.cc}</span>
          <span className="ex-mono lpf-num">{r.n}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * One number fanning out to the people who answer it - a call flow, drawn as the wiring
 * diagram it is rather than as three boxes with arrows between them.
 *
 * The geometry is fixed and the viewBox has no intrinsic size: `preserveAspectRatio` is
 * left at its default so the drawing scales with the card instead of fighting it.
 */
export function RoutingTree() {
  const branches = [
    { y: 18, label: "Sales" },
    { y: 46, label: "Support" },
    { y: 74, label: "Voicemail" },
  ] as const;
  return (
    <div className="lpf lpf-routing" aria-hidden="true">
      <svg viewBox="0 0 220 92" className="lpf-svg" focusable="false">
        {branches.map((b) => (
          <path
            key={b.label}
            d={`M14 46 C 56 46, 56 ${b.y}, 96 ${b.y}`}
            fill="none"
            stroke="hsl(var(--ex-copper-line))"
            strokeWidth="1"
          />
        ))}
        {/* The trunk: the number the call arrives on. */}
        <circle cx="14" cy="46" r="3.5" fill="hsl(var(--ex-copper))" />
        {branches.map((b, i) => (
          <circle
            key={b.label}
            cx="96"
            cy={b.y}
            r="3"
            fill={i === 0 ? "hsl(var(--ex-verdigris))" : "hsl(var(--ex-bone-dim) / 0.45)"}
          />
        ))}
        {branches.map((b, i) => (
          <text
            key={b.label}
            x="108"
            y={b.y + 3.5}
            className="lpf-svg-label"
            fill={
              i === 0 ? "hsl(var(--ex-bone))" : "hsl(var(--ex-bone-dim))"
            }
          >
            {b.label}
          </text>
        ))}
      </svg>
    </div>
  );
}

/**
 * The softphone's call bar, mid-call. The three controls are drawn, not real: they carry
 * no accessible name and the whole fragment is hidden from assistive technology, so there
 * is nothing here for a keyboard to land on and nothing that looks clickable to a reader.
 */
export function DialBar() {
  return (
    <div className="lpf lpf-dial" aria-hidden="true">
      <div className="lpf-dial-head">
        <span className="ex-lamp ex-lamp-live" />
        <span className="lpf-dial-who">Dana Whitfield</span>
        <span className="ex-mono lpf-dial-timer">04:12</span>
      </div>
      <div className="lpf-dial-keys">
        <span className="lpf-key" />
        <span className="lpf-key" />
        <span className="lpf-key is-end" />
      </div>
    </div>
  );
}
