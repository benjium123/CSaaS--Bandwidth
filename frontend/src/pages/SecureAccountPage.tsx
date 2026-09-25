import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { PasskeysCard } from "@/components/settings/PasskeysCard";
import { TotpEnrolment } from "@/components/security/TotpEnrolment";
import { EmailCodeStep } from "@/components/security/EmailCodeStep";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthPlate,
  AuthSurface,
  Field,
  Lamp,
} from "@/components/auth/AuthShell";

/**
 * Owners and admins of an APPROVED workspace (and platform operators) must hold a second
 * factor: an email code, a passkey or an authenticator app. Nobody sees this while signing up
 * or waiting for review - see backend services/second_factor.py.
 *
 * P41 (original note): until it does, Until it does, the API
 * refuses everything except these enrolment calls, so this screen replaces the whole app.
 *
 * Because it is a wall rather than a page, it has to be the most helpful screen in the
 * product: the only ways out are forward (add a factor) or back (sign out), and both are
 * stated plainly. It claims nothing about the account it cannot know - the greeting only
 * appears once `me` has actually loaded.
 */
export function SecureAccountPage() {
  const { api, me, refreshMe, logout } = useAuth();
  const [password, setPassword] = React.useState("");
  const [emailPassword, setEmailPassword] = React.useState("");
  const [enroll, setEnroll] = React.useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function startTotp() {
    setError(null);
    setBusy(true);
    try {
      const res = await api.request<{ secret: string; provisioning_uri: string }>(
        "/api/v1/auth/2fa/enroll",
        { method: "POST", json: { password } },
      );
      setEnroll({ secret: res.secret, uri: res.provisioning_uri });
      setPassword("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    setError(null);
    setBusy(true);
    try {
      await api.request("/api/v1/auth/2fa/activate", { method: "POST", json: { code } });
      await refreshMe();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="One last step · Two-step verification"
        title="Secure your account"
        lede={
          <>
            {me?.is_platform_operator
              ? "Operator accounts can see every customer, so signing in takes one more check"
              : "Your account can now place calls and hold phone numbers, so signing in takes one more check"}
            {me && me.email ? <> for {me.email}</> : null}. Pick whichever suits you. You can add
            another later in Settings, Security.
          </>
        }
        footer={
          <button type="button" className="ex-link" onClick={logout}>
            Sign out
          </button>
        }
      >
        <div className="space-y-5">
          <section className="rounded-[3px] border border-border/70 bg-[hsl(var(--ex-ink-raise)/0.5)] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="ex-label">Option A · Simplest</span>
              <span className="text-xs text-muted-foreground">Code by email</span>
            </div>
            <div className="space-y-3">
              <p className="text-sm leading-relaxed text-muted-foreground">
                Each time you sign in we email you a six-digit code.
              </p>
              <Field label="Your password" hint="So nobody at an unattended screen can change how you sign in.">
                <AuthInput
                  aria-label="Your password for email codes"
                  type="password"
                  autoComplete="current-password"
                  placeholder="Confirm your password"
                  value={emailPassword}
                  onChange={(e) => setEmailPassword(e.target.value)}
                />
              </Field>
              <EmailCodeStep
                email={me?.email}
                disabled={!emailPassword}
                verifyLabel="Turn on email codes"
                send={() =>
                  api.request("/api/v1/auth/2fa/email/enrol/send", {
                    method: "POST",
                    json: { password: emailPassword },
                  })
                }
                verify={async (value) => {
                  await api.request("/api/v1/auth/2fa/email/enrol/activate", {
                    method: "POST",
                    json: { code: value },
                  });
                  await refreshMe();
                }}
              />
            </div>
          </section>

          <section className="rounded-[3px] border border-border/70 bg-[hsl(var(--ex-ink-raise)/0.5)] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="ex-label">Option B · Most secure</span>
              <Lamp state="live">Cannot be phished</Lamp>
            </div>
            <PasskeysCard onAdded={() => void refreshMe()} />
          </section>

          <section className="rounded-[3px] border border-border/70 bg-[hsl(var(--ex-ink-raise)/0.5)] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="ex-label">Option C</span>
              <span className="text-xs text-muted-foreground">Authenticator app</span>
            </div>

            {!enroll ? (
              <div className="space-y-3">
                <Field
                  label="Your password"
                  hint="Confirming your password stops someone using an unattended screen to attach their own authenticator."
                >
                  <AuthInput
                    aria-label="Your password"
                    type="password"
                    autoComplete="current-password"
                    placeholder="Confirm your password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </Field>
                <AuthButton
                  type="button"
                  tone="quiet"
                  onClick={startTotp}
                  disabled={!password || busy}
                >
                  Set up
                </AuthButton>
              </div>
            ) : (
              <div className="space-y-3">
                <TotpEnrolment secret={enroll.secret} uri={enroll.uri} />
                <a className="ex-link" href={enroll.uri}>
                  Open in authenticator app
                </a>
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
                <AuthButton
                  type="button"
                  onClick={activate}
                  disabled={code.length < 6 || busy}
                  block
                >
                  Turn on
                </AuthButton>
              </div>
            )}
          </section>

          {error && <AuthAlert>{error}</AuthAlert>}
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}
