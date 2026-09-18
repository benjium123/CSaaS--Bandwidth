import * as React from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Field,
  StepRail,
} from "@/components/auth/AuthShell";

/**
 * Public self-serve signup.
 *
 * WORK EMAIL ONLY, and the client half of that rule is a COURTESY, not the rule. The
 * server is the authority: this list catches the common consumer providers so somebody
 * typing a gmail address is told before they fill in a password, but it is not a security
 * control and must never behave like one. A domain we do not list still reaches the server,
 * and whatever the server says about it is rendered verbatim - so the two can never
 * disagree in a way that lets the browser's opinion win.
 *
 * Deliberately NOT a gate on submit: if our list is wrong about a legitimate domain, the
 * hint is a sentence rather than a locked button, and the server decides.
 */
const CONSUMER_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
  "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
  "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com", "yandex.com",
  "zoho.com", "fastmail.com", "tutanota.com", "hey.com",
]);

function looksConsumer(email: string): boolean {
  const at = email.lastIndexOf("@");
  if (at < 0) return false;
  return CONSUMER_DOMAINS.has(email.slice(at + 1).trim().toLowerCase());
}

export function SignUpPage() {
  const { api, login } = useAuth();
  const [email, setEmail] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [company, setCompany] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const consumerHint = email.includes("@") && looksConsumer(email);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.request("/api/v1/auth/register", {
        method: "POST",
        json: { email, password, full_name: fullName, company_name: company.trim() || null },
      });
      // Straight in: the account exists, so signing them in here saves a second form and
      // lands them on the second-factor screen, which is the true next step.
      const res = await login(email, password);
      if (res.kind === "error") setError(res.message);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthSurface>
      <AuthPlate
        as="form"
        onSubmit={onSubmit}
        eyebrow="Step 01 · Your account"
        title="Start your workspace"
        lede="Numbers, texts and calls for your business. Set up takes a few minutes; you can send once your business is verified."
        footer={
          <Link to="/" className="ex-link">
            Already have an account? Sign in
          </Link>
        }
      >
        <StepRail steps={["Account", "Secure it", "Verify business"]} active={0} />

        <div className="space-y-4">
          <Field
            label="Work email"
            hint={
              consumerHint
                ? undefined
                : "Use your business address — it's how we reach you about your application."
            }
          >
            <AuthInput
              aria-label="Work email"
              type="email"
              autoComplete="username"
              placeholder="you@yourcompany.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>

          {/* A sentence, not a locked button. The server decides; this only saves someone
              filling in the rest of the form first. */}
          {consumerHint && (
            <AuthNotice>
              That looks like a personal address. Accounts are for businesses, so please use
              your work email — the one at your company's own domain.
            </AuthNotice>
          )}

          <Field label="Your name">
            <AuthInput
              aria-label="Your name"
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
            />
          </Field>

          <Field label="Company" hint="You can change this later.">
            <AuthInput
              aria-label="Company"
              autoComplete="organization"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
            />
          </Field>

          <Field
            label="Password"
            hint="A long passphrase — three or four unrelated words. Passwords that are too short, that look like your email address, or that have appeared in a public breach are refused."
          >
            <AuthInput
              aria-label="Password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>

          {error && <AuthAlert>{error}</AuthAlert>}

          <AuthButton
            type="submit"
            block
            disabled={busy || !email || !password || !fullName}
          >
            {busy ? "Working..." : "Create account"}
          </AuthButton>

          <p className="text-center text-[0.6875rem] leading-relaxed text-muted-foreground">
            Next you'll add a second factor, then verify your business. Calling and texting
            unlock once that's approved.
          </p>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}
