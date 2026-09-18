import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { PasskeysCard } from "@/components/settings/PasskeysCard";
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
 * P41: every account must have an authenticator app or a passkey. Until it does, the API
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
        eyebrow="Required · Second factor"
        title="Secure your account"
        lede={
          <>
            Every account needs a second way to prove it is you
            {me && me.email ? <> — this one signs in as {me.email}</> : null}. Add a passkey, or
            an authenticator app. Afterwards, create recovery codes in Settings, Team, Security
            so a lost phone never locks you out.
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
              <span className="ex-label">Option A · Recommended</span>
              <Lamp state="live">Cannot be phished</Lamp>
            </div>
            <PasskeysCard onAdded={() => void refreshMe()} />
          </section>

          <section className="rounded-[3px] border border-border/70 bg-[hsl(var(--ex-ink-raise)/0.5)] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="ex-label">Option B</span>
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
                <p className="text-sm leading-relaxed text-muted-foreground">
                  Add this key to Google Authenticator, 1Password, Authy or similar, then enter
                  the six-digit code it shows.
                </p>
                <code className="ex-mono block break-all rounded-[3px] border border-border/70 bg-[hsl(var(--ex-ink)/0.8)] p-3 text-[0.6875rem] leading-relaxed text-[hsl(var(--ex-verdigris))]">
                  {enroll.secret}
                </code>
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
