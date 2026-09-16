import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";

/**
 * P42: every passkey and the authenticator app are lost. Owners, admins and billing staff
 * who passed an ID check during business verification prove it is them again with a new
 * ID + selfie. Everyone else asks their workspace admin to reset their sign-in methods.
 *
 * The sign-in's pending token and the identity check id survive the round trip to Stripe
 * in sessionStorage (this tab only).
 */
const KEY = "csaas.recovery";

type Stored = { pendingToken?: string; stepUpId?: string };

function load(): Stored {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) ?? "{}") as Stored;
  } catch {
    return {};
  }
}

function save(next: Stored): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable - the flow still works within this page */
  }
}

export function rememberPendingForRecovery(pendingToken: string): void {
  save({ ...load(), pendingToken });
}

export function RecoverAccountPage() {
  const { api, refreshMe } = useAuth();
  const navigate = useNavigate();
  const [stored] = React.useState<Stored>(load);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function start() {
    if (!stored.pendingToken) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.request<{ step_up_id: string; url: string }>(
        "/api/v1/auth/recovery/identity/start",
        {
          method: "POST",
          json: { pending_token: stored.pendingToken, return_url: `${window.location.origin}/recover` },
        },
      );
      save({ ...stored, stepUpId: res.step_up_id });
      window.location.assign(res.url);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  async function complete() {
    if (!stored.pendingToken || !stored.stepUpId) return;
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/recovery/identity/complete", {
        method: "POST",
        json: { pending_token: stored.pendingToken, step_up_id: stored.stepUpId },
      });
      sessionStorage.removeItem(KEY);
      await refreshMe();
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        `${(err as Error).message} If your sign-in expired, sign in with your password again and choose "Lost access" to continue.`,
      );
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-md space-y-4 rounded-lg border border-border p-6">
        <h1 className="text-lg font-semibold">Recover your account</h1>
        {!stored.pendingToken ? (
          <p className="text-sm">
            Start by signing in with your email and password, then choose "Lost access to your
            passkey and authenticator app".
          </p>
        ) : stored.stepUpId ? (
          <>
            <p className="text-sm">
              Finished the ID and selfie check? Continue to set up a new passkey. For your
              protection, sensitive changes stay locked for 24 hours.
            </p>
            <Button type="button" className="w-full" onClick={complete} disabled={busy}>
              Continue
            </Button>
          </>
        ) : (
          <>
            <p className="text-sm">
              If you are an owner, admin or billing contact whose ID was verified for your
              business, you can recover by taking a photo of the same ID and a selfie.
            </p>
            <p className="text-sm text-muted-foreground">
              Everyone else: ask an admin of your workspace to reset your sign-in methods.
            </p>
            <Button type="button" className="w-full" onClick={start} disabled={busy}>
              Verify with ID and selfie
            </Button>
          </>
        )}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <Link to="/" className="block text-sm underline">Back to sign in</Link>
      </div>
    </div>
  );
}
