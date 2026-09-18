/**
 * The furniture every authentication screen is built from.
 *
 * One layout, one set of controls, so the front door is a single designed surface instead
 * of seven separately-invented boxes. Nothing here knows anything about auth: these are
 * presentation pieces, and every decision about factors, lockouts and error copy stays in
 * the pages and in AuthContext where it can be reviewed.
 *
 * Two rules this file exists to enforce:
 *  - Accessible names are passed straight through. The tests (and screen readers) find
 *    fields by `aria-label` and controls by their exact visible text, so every decorative
 *    glyph in here is `aria-hidden` and no icon is ever part of a control's name.
 *  - Nothing in here states a security property. `AuthAside` lists what the PRODUCT does;
 *    it never says anything about the person signing in, whose account state the client
 *    cannot know until /auth/me has answered.
 *
 * THE RULE FOR EVERY AUTH SCREEN, because this is where the next person writing one will
 * look: `me` from useAuth is null until /auth/me answers, and optional chaining cannot
 * tell "false" from "not asked yet".
 *
 *     me?.x && <Thing/>          SAFE.      Renders on TRUTH. Unknown degrades to nothing.
 *     !me?.x && <Claim/>         DANGEROUS. Renders on FALSITY, which is also the value of
 *                                "we haven't loaded". It states a negative you have not
 *                                earned - "you have no way to confirm", "you are not a
 *                                member of anything" - to someone for whom it is untrue.
 *
 * So guard anything that ASSERTS with an explicit `me == null ? null :` or `me &&`, and
 * leave `me?.x &&` alone: hiding a control while data loads claims nothing. This is audit
 * finding 12 in one line, and it is the difference between OrgPickerPage's membership list
 * (needs the explicit guard) and its operator link (does not).
 */
import * as React from "react";
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/martian-mono/wght.css";
import "@/auth/authTheme.css";
import { cn } from "@/lib/utils";

/** Stagger helper: every revealed element declares its place in the arrival sequence. */
function delay(step: number): React.CSSProperties {
  return { ["--d" as string]: `${step * 70}ms` };
}

/* ---------------------------------------------------------------- the page */

export function AuthSurface({
  children,
  aside,
}: {
  children: React.ReactNode;
  /** Replaced wholesale by `/report`, which is a public safety form, not a sign-in. */
  aside?: React.ReactNode;
}) {
  return (
    // `dark` as well as `auth-surface`: the scope re-points the shared tokens, and the
    // class keeps any `dark:` variant inside a shared primitive behaving correctly.
    <div className="auth-surface dark flex min-h-full w-full flex-col lg:flex-row">
      {aside === undefined ? <AuthAside /> : aside}
      <main className="flex flex-1 items-center justify-center px-5 py-10 sm:px-8 lg:py-16">
        <div className="w-full max-w-[27rem]">{children}</div>
      </main>
    </div>
  );
}

/** The dial tone, drawn once. Decorative: it carries no information. */
function DialTone({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 420 60"
      preserveAspectRatio="none"
      className={cn("ex-trace h-10 w-full", className)}
    >
      <path
        d="M0 30 C 20 30, 25 8, 45 8 S 70 52, 90 52 S 115 8, 135 8 S 160 52, 180 52 S 205 8, 225 8 S 250 52, 270 52 S 295 22, 315 26 S 350 30, 420 30"
        fill="none"
        stroke="hsl(var(--ex-verdigris) / 0.55)"
        strokeWidth="1.25"
      />
    </svg>
  );
}

/** Rows of a legend plate: what the platform does, stated as equipment specification. */
const PLATE: { k: string; v: string }[] = [
  { k: "Sign-in", v: "Passkeys · authenticator app · recovery codes" },
  { k: "Enterprise", v: "SAML 2.0 · OIDC · SCIM user sync" },
  { k: "Sessions", v: "HttpOnly cookie, idle and absolute limits" },
  { k: "Numbers", v: "United States · United Kingdom" },
];

export function AuthAside() {
  return (
    <aside className="relative flex shrink-0 flex-col justify-between gap-10 border-b border-border/60 px-5 py-8 sm:px-8 lg:w-[42%] lg:max-w-xl lg:border-b-0 lg:border-r lg:py-16 lg:pl-14">
      <div>
        <div className="ex-rise flex items-baseline gap-3" style={delay(0)}>
          <span className="ex-nameplate text-2xl text-[hsl(var(--ex-bone))]">CSaaS</span>
          <span className="ex-label">Console</span>
        </div>
        <p
          className="ex-rise ex-nameplate mt-8 max-w-[22ch] text-balance text-[1.75rem] text-[hsl(var(--ex-bone))] sm:text-[2.125rem] lg:mt-14"
          style={delay(1)}
        >
          {/* A non-breaking hyphen (U+2011) in "sign-in": with `text-balance` the browser
              will otherwise break the line at an ordinary hyphen and leave "sign-" hanging. */}
          Every call, message and sign{"‑"}in
          <span className="text-[hsl(var(--ex-copper))]"> on one line.</span>
        </p>
        <p
          className="ex-rise mt-4 max-w-sm text-sm leading-relaxed text-muted-foreground"
          style={delay(2)}
        >
          Communications software for teams that answer the phone. The switchboard is yours;
          the wiring is ours.
        </p>
      </div>

      <div className="ex-rise hidden lg:block" style={delay(3)}>
        <DialTone />
        <hr className="ex-hairline my-6" />
        <dl className="space-y-3">
          {PLATE.map((row) => (
            <div key={row.k} className="grid grid-cols-[7.5rem_1fr] items-baseline gap-3">
              <dt className="ex-label">{row.k}</dt>
              <dd className="text-[0.8125rem] leading-snug text-muted-foreground">{row.v}</dd>
            </div>
          ))}
        </dl>
      </div>
    </aside>
  );
}

/* --------------------------------------------------------------- the plate */

export function AuthPlate({
  eyebrow,
  title,
  lede,
  children,
  footer,
  as = "div",
  ...rest
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  lede?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  as?: "div" | "form";
  // `title` is omitted from the passed-through attributes on purpose: the DOM's own
  // `title` is a string tooltip, and ours is the heading, which is often an element.
} & Omit<React.FormHTMLAttributes<HTMLFormElement>, "title">) {
  // createElement rather than a `<Tag>` alias: a <form>'s props and a <div>'s props are
  // not assignable to one another, and casting the element type is tidier than widening
  // every handler.
  const plate = React.createElement(
    as,
    { ...rest, className: "ex-plate ex-rise px-6 py-7 sm:px-8", style: delay(1) },
    <header key="h" className="mb-6">
      {eyebrow != null ? <div className="ex-label mb-2.5">{eyebrow}</div> : null}
      <h1 className="ex-nameplate text-[1.5rem] text-[hsl(var(--ex-bone))]">{title}</h1>
      {lede != null ? (
        <p className="mt-2 text-[0.8125rem] leading-relaxed text-muted-foreground">{lede}</p>
      ) : null}
    </header>,
    <React.Fragment key="b">{children}</React.Fragment>,
  );

  return (
    <>
      {plate}
      {footer != null ? (
        <div className="ex-rise mt-5 flex flex-col gap-2" style={delay(3)}>
          {footer}
        </div>
      ) : null}
    </>
  );
}

/**
 * Where the sign-in has got to. Purely a position indicator - it never claims a step is
 * complete on its own reasoning, the page passes `active` from state it actually has.
 */
export function StepRail({ steps, active }: { steps: string[]; active: number }) {
  return (
    <ol className="mb-6 flex items-center gap-2" aria-hidden="true">
      {steps.map((label, i) => (
        <li key={label} className="flex flex-1 items-center gap-2">
          <span
            className={cn(
              "ex-lamp",
              i < active && "ex-lamp-live",
              i === active && "ex-lamp-wait",
            )}
          />
          <span
            className={cn(
              "ex-label truncate",
              i === active && "text-[hsl(var(--ex-bone))]",
            )}
          >
            {label}
          </span>
        </li>
      ))}
    </ol>
  );
}

/* -------------------------------------------------------------- the pieces */

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="ex-label mb-2 block">{label}</span>
      {children}
      {hint != null ? (
        <span className="mt-2 block text-xs leading-relaxed text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  );
}

export const AuthInput = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement> & { code?: boolean }
>(({ className, code, ...props }, ref) => (
  <input ref={ref} className={cn("ex-input", code && "ex-code", className)} {...props} />
));
AuthInput.displayName = "AuthInput";

type ButtonTone = "primary" | "quiet" | "key";

export const AuthButton = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: ButtonTone; block?: boolean }
>(({ className, tone = "primary", block, ...props }, ref) => (
  <button
    ref={ref}
    className={cn("ex-btn", `ex-btn-${tone}`, block && "w-full", className)}
    {...props}
  />
));
AuthButton.displayName = "AuthButton";

/**
 * Failure text. `role="alert"` and nothing else: the message is the server's, rendered
 * verbatim. This component deliberately has no variants keyed on an error code - a
 * failed sign-in must read identically whatever caused it, or the screen becomes an
 * oracle for which email addresses exist.
 */
export function AuthAlert({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="ex-alert">
      {children}
    </p>
  );
}

export function AuthNotice({ children }: { children: React.ReactNode }) {
  return <p className="ex-notice">{children}</p>;
}

/** A named state with a lamp beside it. The text always says it too. */
export function Lamp({
  state,
  children,
}: {
  state: "live" | "wait" | "fault" | "idle";
  children: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-2 text-[0.8125rem] text-muted-foreground">
      <span className={cn("ex-lamp", state !== "idle" && `ex-lamp-${state}`)} />
      {children}
    </span>
  );
}

export { delay as authDelay };
