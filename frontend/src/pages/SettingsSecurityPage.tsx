import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";

export function SettingsSecurityPage() {
  const { api, me } = useAuth();
  const [enroll, setEnroll] = React.useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = React.useState("");
  const [message, setMessage] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [enrolling, setEnrolling] = React.useState(false);
  const [activating, setActivating] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  // Item 8: the Disable-2FA panel only makes sense when 2FA is actually on - gated on
  // /auth/me's `totp_enabled` (undefined, i.e. backend hasn't shipped it yet for this
  // user, is treated as false/not-enabled rather than showing the panel regardless).
  const totpEnabled = Boolean(me?.totp_enabled);

  const [disableCode, setDisableCode] = React.useState("");
  const [disablePassword, setDisablePassword] = React.useState("");
  const [disableError, setDisableError] = React.useState<string | null>(null);
  const [disabling, setDisabling] = React.useState(false);

  async function startEnroll() {
    setError(null);
    setEnrolling(true);
    try {
      const res = await api.request<{ secret: string; provisioning_uri: string }>(
        "/api/v1/auth/2fa/enroll",
        { method: "POST" },
      );
      setEnroll({ secret: res.secret, uri: res.provisioning_uri });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setEnrolling(false);
    }
  }

  async function activate() {
    setError(null);
    setActivating(true);
    try {
      await api.request("/api/v1/auth/2fa/activate", { method: "POST", json: { code } });
      setMessage("Two-factor authentication is on.");
      setEnroll(null);
      setCode("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActivating(false);
    }
  }

  async function copyUri() {
    if (!enroll) return;
    try {
      await navigator.clipboard.writeText(enroll.uri);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable - the link and secret are still visible to copy by hand */
    }
  }

  async function disable() {
    setDisableError(null);
    setDisabling(true);
    try {
      // Item 8: the password field is always visible while this panel shows (2FA is on)
      // - only send `password` when the user actually filled it in, since the backend
      // treats it as optional depending on the account.
      const json: { code: string; password?: string } = { code: disableCode };
      if (disablePassword) json.password = disablePassword;
      await api.request("/api/v1/auth/2fa/disable", { method: "POST", json });
      setMessage("Two-factor authentication is off.");
      setDisableCode("");
      setDisablePassword("");
    } catch (err) {
      setDisableError((err as Error).message);
    } finally {
      setDisabling(false);
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
        <Button onClick={startEnroll} disabled={enrolling}>
          Set up two-factor authentication
        </Button>
      ) : (
        <div className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm">
            Add this secret to your authenticator app, then enter the six-digit code.
          </p>
          <code className="block break-all rounded bg-muted p-2 text-xs">{enroll.secret}</code>
          <p className="text-sm">
            Or open it directly:{" "}
            <a className="underline" href={enroll.uri}>
              {enroll.uri}
            </a>
          </p>
          <div className="flex gap-2">
            <Input
              readOnly
              aria-label="Provisioning URI"
              value={enroll.uri}
              onFocus={(e) => e.currentTarget.select()}
            />
            <Button type="button" variant="outline" onClick={copyUri}>
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <div className="flex gap-2">
            <Input
              aria-label="Authenticator code"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              disabled={activating}
            />
            <Button onClick={activate} disabled={code.length < 6 || activating}>
              Activate
            </Button>
          </div>
        </div>
      )}

      {totpEnabled && (
        <div className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm font-medium">Disable two-factor authentication</p>
          {disableError && (
            <p role="alert" className="text-sm text-destructive">
              {disableError}
            </p>
          )}
          <div className="flex gap-2">
            <Input
              aria-label="Confirmation code"
              inputMode="numeric"
              value={disableCode}
              onChange={(e) => setDisableCode(e.target.value)}
              disabled={disabling}
            />
            <Input
              aria-label="Password"
              type="password"
              value={disablePassword}
              onChange={(e) => setDisablePassword(e.target.value)}
              disabled={disabling}
            />
            <Button
              variant="outline"
              onClick={disable}
              disabled={disableCode.length < 6 || disabling}
            >
              Disable 2FA
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
