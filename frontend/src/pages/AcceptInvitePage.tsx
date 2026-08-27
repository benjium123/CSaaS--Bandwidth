import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";

export function AcceptInvitePage() {
  const { api, login } = useAuth();
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
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.request("/api/v1/auth/register", {
        method: "POST",
        json: { email, password, full_name: fullName, invite_token: token },
      });
      const res = await login(email, password);
      if (res.kind === "error") setError(res.message);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <div className="w-full max-w-sm space-y-3 rounded-lg border border-border p-6 text-sm">
          <p>This invitation link is missing its token.</p>
          <a className="underline" href="/">
            Back to sign in
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6"
      >
        <h1 className="text-lg font-semibold">Accept your invitation</h1>

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
          <span className="text-sm text-muted-foreground">Full name</span>
          <Input
            aria-label="Full name"
            autoComplete="name"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted-foreground">Password</span>
          <Input
            aria-label="Password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <span className="block text-xs text-muted-foreground">At least 10 characters.</span>
        </label>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <Button type="submit" disabled={busy} className="w-full">
          {busy ? "Working..." : "Create account"}
        </Button>
      </form>
    </div>
  );
}
