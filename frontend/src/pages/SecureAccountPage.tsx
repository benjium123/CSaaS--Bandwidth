import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";
import { PasskeysCard } from "@/components/settings/PasskeysCard";

/**
 * P41: every account must have an authenticator app or a passkey. Until it does, the API
 * refuses everything except these enrolment calls, so this screen replaces the whole app.
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
    <div className="dark flex min-h-full items-center justify-center bg-background p-6 text-foreground">
      <div className="w-full max-w-lg space-y-6 rounded-lg border border-border p-6">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Secure your account</h1>
          <p className="text-sm text-muted-foreground">
            Every account needs a second way to prove it is you. Add a passkey (recommended) or
            an authenticator app to continue{me?.email ? ` as ${me.email}` : ""}.
          </p>
        </div>

        <section className="space-y-3 rounded-md border border-border p-4">
          <PasskeysCard onAdded={() => void refreshMe()} />
        </section>

        <section className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm font-medium">Authenticator app</p>
          {!enroll ? (
            <div className="flex gap-2">
              <Input
                aria-label="Your password"
                type="password"
                autoComplete="current-password"
                placeholder="Confirm your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <Button type="button" onClick={startTotp} disabled={!password || busy}>
                Set up
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-sm">
                Add this key to Google Authenticator, 1Password, Authy or similar, then enter the
                six-digit code it shows.
              </p>
              <code className="block break-all rounded bg-muted p-2 text-xs">{enroll.secret}</code>
              <a className="text-sm underline" href={enroll.uri}>
                Open in authenticator app
              </a>
              <div className="flex gap-2">
                <Input
                  aria-label="Authenticator code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
                <Button type="button" onClick={activate} disabled={code.length < 6 || busy}>
                  Turn on
                </Button>
              </div>
            </div>
          )}
        </section>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <Button type="button" variant="ghost" onClick={logout}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
