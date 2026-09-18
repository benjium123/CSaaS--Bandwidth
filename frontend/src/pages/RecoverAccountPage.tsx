import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import {
  AuthAlert,
  AuthButton,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Lamp,
  StepRail,
} from "@/components/auth/AuthShell";

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
  const [stored, setStored] = React.useState<Stored>(load);
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
          json: {
            pending_token: stored.pendingToken,
            return_url: `${window.location.origin}/recover`,
          },
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

  /**
   * The way out of a failed attempt, and the reason it appears ONLY after one: it clears
   * the stored pending token, which is the one piece of state this flow cannot recreate.
   * Offered unprompted it would be a trapdoor - a button that quietly discards a sign-in
   * still in progress. Offered after the server has refused, the token is already spent
   * and clearing it is the only thing that unsticks the page.
   */
  function startOver() {
    try {
      sessionStorage.removeItem(KEY);
    } catch {
      /* ignore - the state below is reset either way */
    }
    setStored({});
    setError(null);
    navigate("/", { replace: true });
  }

  const stage = !stored.pendingToken ? 0 : stored.stepUpId ? 2 : 1;

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Recovery · Identity"
        title="Recover your account"
        lede="For when the passkey and the authenticator app are both gone."
        footer={
          <>
            <Link to="/" className="ex-link">
              Back to sign in
            </Link>
            {error ? (
              <button type="button" className="ex-link" onClick={startOver}>
                Start this recovery again
              </button>
            ) : null}
          </>
        }
      >
        <StepRail steps={["Sign in", "Prove identity", "New passkey"]} active={stage} />

        <div className="space-y-4">
          {stage === 0 ? (
            <AuthNotice>
              Start by signing in with your email and password, then choose "Lost access to your
              passkey and authenticator app".
            </AuthNotice>
          ) : stage === 2 ? (
            <>
              <p className="text-sm leading-relaxed text-muted-foreground">
                Finished the ID and selfie check? Continue to set up a new passkey. For your
                protection, sensitive changes stay locked for 24 hours.
              </p>
              <AuthButton type="button" block onClick={complete} disabled={busy}>
                Continue
              </AuthButton>
              {busy ? <Lamp state="wait">Checking your identity result.</Lamp> : null}
            </>
          ) : (
            <>
              <p className="text-sm leading-relaxed text-muted-foreground">
                If you are an owner, admin or billing contact whose ID was verified for your
                business, you can recover by taking a photo of the same ID and a selfie.
              </p>
              <p className="text-sm leading-relaxed text-muted-foreground">
                Everyone else: ask an admin of your workspace to reset your sign-in methods.
              </p>
              <AuthButton type="button" block onClick={start} disabled={busy}>
                Verify with ID and selfie
              </AuthButton>
            </>
          )}

          {error && <AuthAlert>{error}</AuthAlert>}
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}
