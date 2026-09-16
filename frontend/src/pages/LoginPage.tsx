import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { rememberPendingForRecovery } from "@/pages/RecoverAccountPage";
import { Button, Input } from "@/components/ui/primitives";

export function LoginPage() {
  const { login, verify2fa, verifyPasskey, recoverWithCode } = useAuth();
  const navigate = useNavigate();
  const [useRecoveryCode, setUseRecoveryCode] = React.useState(false);
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [code, setCode] = React.useState("");
  const [pendingToken, setPendingToken] = React.useState<string | null>(null);
  const [methods, setMethods] = React.useState<string[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [ssoOpen, setSsoOpen] = React.useState(false);
  const [orgSlug, setOrgSlug] = React.useState("");

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    const res = pendingToken
      ? useRecoveryCode
        ? await recoverWithCode(pendingToken, code)
        : await verify2fa(pendingToken, code)
      : await login(email, password);
    setBusy(false);

    if (res.kind === "needs_2fa") {
      setPendingToken(res.pendingToken);
      setMethods(res.methods);
      return;
    }
    if (res.kind === "error") setError(res.message);
  }

  async function onPasskey() {
    if (!pendingToken) return;
    setError(null);
    setBusy(true);
    const res = await verifyPasskey(pendingToken);
    setBusy(false);
    if (res.kind === "error") setError(res.message);
  }

  const totpAllowed = !pendingToken || methods.includes("totp") || useRecoveryCode;
  const passkeyAllowed = Boolean(pendingToken) && methods.includes("passkey") && !useRecoveryCode;

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-4">
        <form
          onSubmit={onSubmit}
          className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6"
        >
          <h1 className="text-lg font-semibold">
            {pendingToken ? "Two-factor code" : "Sign in"}
          </h1>

          {pendingToken ? (
            <>
              {passkeyAllowed && (
                <Button type="button" className="w-full" disabled={busy} onClick={onPasskey}>
                  Use your passkey
                </Button>
              )}
              {passkeyAllowed && totpAllowed && (
                <p className="text-center text-xs text-muted-foreground">or</p>
              )}
              {totpAllowed && (
                <label className="block space-y-1">
                  <span className="text-sm text-muted-foreground">
                    {useRecoveryCode ? "Recovery code" : "Authenticator code"}
                  </span>
                  <Input
                    aria-label={useRecoveryCode ? "Recovery code" : "Authenticator code"}
                    inputMode={useRecoveryCode ? "text" : "numeric"}
                    autoComplete="one-time-code"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                </label>
              )}
            </>
          ) : (
            <>
              <label className="block space-y-1">
                <span className="text-sm text-muted-foreground">Email</span>
                <Input
                  aria-label="Email"
                  type="email"
                  autoComplete="username"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label className="block space-y-1">
                <span className="text-sm text-muted-foreground">Password</span>
                <Input
                  aria-label="Password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
            </>
          )}

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          {totpAllowed && (
            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Working..." : pendingToken ? "Verify" : "Sign in"}
            </Button>
          )}

          {pendingToken ? (
            <div className="space-y-1 text-sm">
              <button
                type="button"
                className="block underline"
                onClick={() => {
                  setUseRecoveryCode((v) => !v);
                  setCode("");
                }}
              >
                {useRecoveryCode ? "Use my passkey or authenticator app" : "Use a recovery code"}
              </button>
              <button
                type="button"
                className="block underline"
                onClick={() => {
                  rememberPendingForRecovery(pendingToken);
                  navigate("/recover");
                }}
              >
                Lost access to your passkey and authenticator app
              </button>
            </div>
          ) : (
            <Link to="/forgot-password" className="block text-sm underline">
              Forgot your password?
            </Link>
          )}
        </form>

        {!pendingToken && (
          <div className="border-t border-border pt-4">
            {!ssoOpen ? (
              <Button type="button" variant="outline" onClick={() => setSsoOpen(true)}>
                Sign in with SSO
              </Button>
            ) : (
              <div className="space-y-3">
                <label className="block space-y-1">
                  <span className="text-sm text-muted-foreground">Workspace short name</span>
                  <Input
                    aria-label="Workspace short name"
                    placeholder="acme"
                    value={orgSlug}
                    onChange={(e) => setOrgSlug(e.target.value)}
                  />
                  <span className="text-xs text-muted-foreground">
                    Your workspace's short name, from its sign-in link.
                  </span>
                </label>
                {/* A plain link, not a fetch: the endpoint answers a 302 to the identity
                    provider, so the browser itself has to follow it. Going through the API
                    client would turn the hand-off into a cross-origin XHR and lose the flow. */}
                {orgSlug.trim() ? (
                  <a
                    className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm"
                    href={`/api/v1/auth/sso/${encodeURIComponent(orgSlug.trim())}/start`}
                  >
                    Continue
                  </a>
                ) : (
                  <Button type="button" disabled>
                    Continue
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
