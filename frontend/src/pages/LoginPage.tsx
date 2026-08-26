import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";

export function LoginPage() {
  const { login, verify2fa } = useAuth();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [code, setCode] = React.useState("");
  const [pendingToken, setPendingToken] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    const res = pendingToken
      ? await verify2fa(pendingToken, code)
      : await login(email, password);
    setBusy(false);

    if (res.kind === "needs_2fa") {
      setPendingToken(res.pendingToken);
      return;
    }
    if (res.kind === "error") setError(res.message);
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6"
      >
        <h1 className="text-lg font-semibold">
          {pendingToken ? "Two-factor code" : "Sign in"}
        </h1>

        {pendingToken ? (
          <label className="block space-y-1">
            <span className="text-sm text-muted-foreground">Authenticator code</span>
            <Input
              aria-label="Authenticator code"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </label>
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

        <Button type="submit" disabled={busy} className="w-full">
          {busy ? "Working..." : pendingToken ? "Verify" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
