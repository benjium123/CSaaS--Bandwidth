import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
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

export function SignUpPage() {
  const { api, login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.request("/api/v1/auth/register", {
        method: "POST",
        json: {
          email,
          password,
          full_name: fullName.trim(),
          company_name: "",
          account_type: "individual",
        },
      });
      // Straight in: the account exists, so signing them in here saves a second form and
      // lands them in onboarding. A second factor is handled by the global 2FA gate, which
      // owns the screen, so needs_2fa deliberately navigates nowhere.
      const res = await login(email, password);
      if (res.kind === "error") {
        setError(res.message);
      } else if (res.kind === "ok") {
        navigate("/onboarding", { replace: true });
      }
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
        title="Create your Ringlite account"
        lede="Use your personal or work email. Verify your identity, get approved, then choose your phone numbers and start calling."
        footer={
          <Link to="/login" className="ex-link">
            Already have an account? Sign in
          </Link>
        }
      >
        <StepRail steps={["Account", "Confirm email", "Verify identity"]} active={0} />
        <div className="space-y-4">
          <AuthNotice>Texting unlocks after you register a company and receive approval for a 10DLC campaign.</AuthNotice>
          <Field label="Email" hint="Personal and work email addresses are welcome.">
            <AuthInput aria-label="Email" type="email" autoComplete="username" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>

          <Field label="Your name">
            <AuthInput
              aria-label="Your name"
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
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
            disabled={busy || !email || !password || !fullName.trim()}
          >
            {busy ? "Working..." : "Create account"}
          </AuthButton>

          <p className="text-center text-[0.6875rem] leading-relaxed text-muted-foreground">
            Next, confirm your email and verify your identity. Decisions typically arrive within one hour.
          </p>
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}
