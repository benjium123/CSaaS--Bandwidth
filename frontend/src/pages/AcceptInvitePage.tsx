import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthPlate,
  AuthSurface,
  Field,
  StepRail,
} from "@/components/auth/AuthShell";

export function AcceptInvitePage() {
  const { api, login, verify2fa } = useAuth();
  // Read once from the raw URL rather than through react-router state: this page must
  // work whether or not the surrounding app has established router context yet (it is
  // reachable before login - see App.tsx).
  const token = React.useMemo(
    () => new URLSearchParams(window.location.search).get("token") ?? "",
    [],
  );

  const [email, setEmail] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [code, setCode] = React.useState("");
  const [pendingToken, setPendingToken] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      // The 2FA step, if any, comes back from login() below - registration itself
      // never asks for a code.
      if (!pendingToken) {
        await api.request("/api/v1/auth/register", {
          method: "POST",
          json: { email, password, full_name: fullName, invite_token: token },
        });
      }
      const res = pendingToken ? await verify2fa(pendingToken, code) : await login(email, password);
      if (res.kind === "needs_2fa") {
        setPendingToken(res.pendingToken);
        return;
      }
      if (res.kind === "error") setError(res.message);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <AuthSurface>
        <AuthPlate
          eyebrow="Invitation"
          title="This link is incomplete"
          footer={
            <a className="ex-link" href="/">
              Back to sign in
            </a>
          }
        >
          <p className="text-sm leading-relaxed text-muted-foreground">
            This invitation link is missing its token. Ask whoever invited you to send it again -
            the whole link, including everything after the question mark.
          </p>
        </AuthPlate>
      </AuthSurface>
    );
  }

  return (
    <AuthSurface>
      <AuthPlate
        as="form"
        onSubmit={onSubmit}
        eyebrow={pendingToken ? "Invitation · Second factor" : "Invitation · Your account"}
        title={pendingToken ? "Confirm it is you" : "Accept your invitation"}
        lede={
          pendingToken
            ? "This workspace asks for a second factor. Enter the code from your authenticator app."
            : "Your account is yours, not the workspace's. It follows you if you are invited to another."
        }
      >
        <StepRail steps={["Your account", "Second factor"]} active={pendingToken ? 1 : 0} />

        {pendingToken ? (
          <Field label="Authenticator code">
            <AuthInput
              code
              aria-label="Authenticator code"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </Field>
        ) : (
          <div className="space-y-4">
            <Field label="Email">
              <AuthInput
                aria-label="Email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <Field label="Full name">
              <AuthInput
                aria-label="Full name"
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
            </Field>
            {/* No character count in the hint, deliberately: the minimum is a deployment
             * setting, and the policy also refuses anything found in a public breach - so a
             * number here would both go stale and promise an acceptance the server will not
             * honour. The shape of a good passphrase is stated; the server's refusal is
             * rendered verbatim above and is the only authority. */}
            <Field
              label="Password"
              hint="A long passphrase - three or four unrelated words. Passwords that are too short, that look like your email address, or that have appeared in a public breach are refused."
            >
              <AuthInput
                aria-label="Password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          </div>
        )}

        {error && (
          <div className="mt-4">
            <AuthAlert>{error}</AuthAlert>
          </div>
        )}

        <AuthButton type="submit" block disabled={busy} className="mt-5">
          {busy ? "Working..." : pendingToken ? "Verify" : "Create account"}
        </AuthButton>
      </AuthPlate>
    </AuthSurface>
  );
}
