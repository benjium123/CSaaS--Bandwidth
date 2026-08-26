import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";

export function SettingsSecurityPage() {
  const { api } = useAuth();
  const [enroll, setEnroll] = React.useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = React.useState("");
  const [message, setMessage] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function startEnroll() {
    setError(null);
    try {
      const res = await api.request<{ secret: string; provisioning_uri: string }>(
        "/api/v1/auth/2fa/enroll",
        { method: "POST" },
      );
      setEnroll({ secret: res.secret, uri: res.provisioning_uri });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function activate() {
    setError(null);
    try {
      await api.request("/api/v1/auth/2fa/activate", { method: "POST", json: { code } });
      setMessage("Two-factor authentication is on.");
      setEnroll(null);
      setCode("");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Security</h1>

      {message && <p className="text-sm">{message}</p>}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {!enroll ? (
        <Button onClick={startEnroll}>Set up two-factor authentication</Button>
      ) : (
        <div className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm">
            Add this secret to your authenticator app, then enter the six-digit code.
          </p>
          <code className="block break-all rounded bg-muted p-2 text-xs">{enroll.secret}</code>
          <div className="flex gap-2">
            <Input
              aria-label="Authenticator code"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <Button onClick={activate} disabled={code.length < 6}>
              Activate
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
