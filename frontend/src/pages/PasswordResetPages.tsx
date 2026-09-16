import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";

/** P42: request a reset link. Always shows the same confirmation, whether or not the
 * email has an account. */
export function ForgotPasswordPage() {
  const { api } = useAuth();
  const [email, setEmail] = React.useState("");
  const [sent, setSent] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/password/forgot", { method: "POST", json: { email } });
      setSent(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6">
        <h1 className="text-lg font-semibold">Reset your password</h1>
        {sent ? (
          <p className="text-sm">
            If an account exists for that email, a reset link is on its way. It works once and
            expires soon. You will still need your passkey or authenticator app to sign in.
          </p>
        ) : (
          <>
            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">Email</span>
              <Input aria-label="Email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full" disabled={!email || busy}>
              Send reset link
            </Button>
          </>
        )}
        <Link to="/" className="block text-sm underline">Back to sign in</Link>
      </form>
    </div>
  );
}

export function ResetPasswordPage() {
  const { api } = useAuth();
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("The passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/password/reset", {
        method: "POST",
        json: { token, new_password: password },
      });
      setDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6">
        <h1 className="text-lg font-semibold">Choose a new password</h1>
        {done ? (
          <p className="text-sm">
            Your password was changed and every device was signed out. Sign in with the new
            password and your passkey or authenticator app.
          </p>
        ) : !token ? (
          <p role="alert" className="text-sm text-destructive">This reset link is incomplete.</p>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              Use at least 12 characters. A few unrelated words is strong and easy to remember.
            </p>
            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">New password</span>
              <Input aria-label="New password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">Confirm new password</span>
              <Input aria-label="Confirm new password" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </label>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full" disabled={!password || busy}>
              Save new password
            </Button>
          </>
        )}
        <Link to="/" className="block text-sm underline">Back to sign in</Link>
      </form>
    </div>
  );
}
