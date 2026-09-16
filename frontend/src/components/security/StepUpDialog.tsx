import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input } from "@/components/ui/primitives";
import { getPasskeyAssertion, passkeysSupported } from "@/lib/webauthn";

type Pending = { kind: string; action: string; message: string };

export const ACTION_LABELS: Record<string, string> = {
  payment_method_change: "change how your business pays",
  limit_increase: "request higher limits",
  bulk_number_order: "order more numbers",
  api_key_create: "create an API key",
  admin_grant: "give someone admin or billing access",
  ownership_transfer: "make someone an owner",
  use_case_change: "change what your business uses calling and texting for",
  operator_console: "use the operator console",
  suspend: "suspend an account",
  unsuspend: "lift a suspension",
  ban: "change the ban list",
};

/**
 * P41: when the API refuses an action with step_up_required, this dialog asks the person to
 * prove it is them - authenticator code / passkey for recent_2fa, or a Stripe ID + selfie
 * check for recent_selfie - and then tells them to try the action again.
 */
export function StepUpDialog() {
  const { api, me } = useAuth();
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [code, setCode] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState(false);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    api.onStepUpRequired = (details) => {
      setPending(details);
      setDone(false);
      setError(null);
      setCode("");
    };
    return () => {
      api.onStepUpRequired = undefined;
    };
  }, [api]);

  if (!pending) return null;
  const what = ACTION_LABELS[pending.action] ?? "continue";

  async function confirmWithCode() {
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/2fa/step-up", { method: "POST", json: { code } });
      setDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmWithPasskey() {
    setBusy(true);
    setError(null);
    try {
      const opts = await api.request<{ challenge_id: string; options: unknown }>(
        "/api/v1/auth/passkeys/step-up/options",
        { method: "POST" },
      );
      const credential = await getPasskeyAssertion(opts.options);
      await api.request("/api/v1/auth/passkeys/step-up/verify", {
        method: "POST",
        json: { challenge_id: opts.challenge_id, credential },
      });
      setDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function startSelfie() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.request<{ url: string }>("/api/v1/kyc/step-up", {
        method: "POST",
        json: { action: pending!.action, return_url: window.location.href },
      });
      window.location.assign(res.url);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="step-up-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    >
      <div className="dark w-full max-w-md space-y-4 rounded-lg border border-border bg-background p-6 text-foreground">
        <h2 id="step-up-title" className="text-base font-semibold">
          {done ? "Thanks - you're confirmed" : "Confirm it's you"}
        </h2>

        {done ? (
          <p className="text-sm text-muted-foreground">Go ahead and try that again.</p>
        ) : pending.kind === "recent_selfie" ? (
          <>
            <p className="text-sm text-muted-foreground">
              To {what}, take a quick photo of your ID and a selfie. It takes about a minute
              and protects your business if someone else gets into your account.
            </p>
            <Button type="button" onClick={startSelfie} disabled={busy} className="w-full">
              Verify with ID and selfie
            </Button>
          </>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              To {what}, confirm with your passkey or authenticator app.
            </p>
            {me?.has_passkey && passkeysSupported() && (
              <Button type="button" onClick={confirmWithPasskey} disabled={busy} className="w-full">
                Use your passkey
              </Button>
            )}
            {me?.totp_enabled && (
              <div className="flex gap-2">
                <Input
                  aria-label="Authenticator code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
                <Button type="button" onClick={confirmWithCode} disabled={code.length < 6 || busy}>
                  Confirm
                </Button>
              </div>
            )}
          </>
        )}

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={() => setPending(null)}>
            {done ? "Close" : "Cancel"}
          </Button>
        </div>
      </div>
    </div>
  );
}
