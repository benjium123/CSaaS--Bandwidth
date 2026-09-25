import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { getPasskeyAssertion, passkeysSupported } from "@/lib/webauthn";
import { EmailCodeStep } from "@/components/security/EmailCodeStep";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthPlate,
  Field,
  Lamp,
} from "@/components/auth/AuthShell";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { cn } from "@/lib/utils";

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
  password_change: "change your password",
  recovery_codes: "create recovery codes",
  member_reset: "reset a member's sign-in methods",
  user_support: "change a customer's account",
};

/**
 * P41: when the API refuses an action with step_up_required, this dialog asks the person to
 * prove it is them - authenticator code / passkey for recent_2fa, or a Stripe ID + selfie
 * check for recent_selfie - and then tells them to try the action again.
 *
 * THE LOGIC BELOW IS UNCHANGED from the version two audit findings landed on, and the
 * guards are the findings: nothing is claimed about someone's factors until `me` is
 * non-null, and no branch ends without a control. This pass restyled it and split the copy
 * for the two step-up kinds - they are different promises, and the dialog used to blur
 * them - but did not move a single decision.
 *
 * `ACTION_LABELS` is a lookup with a generic fallback on purpose: the backend adds action
 * strings (monitor_unpause arrived after this table was written) and the console must meet
 * an unknown one with a plain sentence, never by reciting a raw identifier at someone in
 * the middle of a security prompt.
 */
export function StepUpDialog() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts: this is a shared store, so the toggle in the sidebar moves
  // every wrapper in the console on the same commit rather than only its own.
  const { theme } = useSurfaceTheme();
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
      // A scrim, like AssignOwnerDrawer's: black at opacity is correct in BOTH themes and
      // is not a palette hue, so it takes no token. The `hsl(197 40% 2% / .72)` that was
      // here was a raw literal doing the same job with a teal cast nothing else shares.
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-[2px]"
    >
      {/* The security surfaces' own scope: a step-up is the same kind of moment as the
          front door, and it should look like it rather than like an ordinary dialog. */}
      <div className={cn("auth-surface", surfaceThemeClass(theme), "w-full max-w-md bg-transparent")}>
        <AuthPlate
          eyebrow={done ? "Confirmed" : "Confirm it's you"}
          title={
            <span id="step-up-title">{done ? "Thanks - you're confirmed" : "Confirm it's you"}</span>
          }
        >
          {done ? (
            <div className="space-y-4">
              <Lamp state="live">This session is confirmed.</Lamp>
              <p className="text-sm text-muted-foreground">Go ahead and try that again.</p>
            </div>
          ) : pending.kind === "passkey_session" ? (
            <div className="space-y-4">
              {/* Nothing is claimed until `me` has loaded: `!me?.x` cannot tell "they don't
                  have it" from "we haven't asked yet", and guessing wrong here tells someone
                  who owns a passkey to go and add one - and throws away their pending
                  step-up when they press the button. */}
              <p className="text-sm leading-relaxed text-muted-foreground">
                Admin and billing features need a passkey sign-in.{" "}
                {!me
                  ? ""
                  : me.has_passkey
                    ? "Confirm with your passkey to continue."
                    : "Add a passkey first."}
              </p>
              {!me ? null : me.has_passkey && passkeysSupported() ? (
                <AuthButton type="button" tone="key" block onClick={confirmWithPasskey} disabled={busy}>
                  Use your passkey
                </AuthButton>
              ) : (
                <AuthButton
                  type="button"
                  block
                  onClick={() => {
                    setPending(null);
                    window.location.assign("/settings/team?tab=security");
                  }}
                >
                  Add a passkey
                </AuthButton>
              )}
            </div>
          ) : pending.kind === "recent_selfie" ? (
            <div className="space-y-4">
              {/* recent_selfie is bound to THIS action - it is not a window that unlocks
                  everything else - so the copy names the action and says so. */}
              <p className="text-sm leading-relaxed text-muted-foreground">
                To {what}, take a quick photo of your ID and a selfie. It takes about a minute
                and protects your business if someone else gets into your account.
              </p>
              <p className="text-xs text-muted-foreground">
                This check covers this one action.
              </p>
              <AuthButton type="button" block onClick={startSelfie} disabled={busy}>
                Verify with ID and selfie
              </AuthButton>
              {busy ? <Lamp state="wait">Opening the identity check.</Lamp> : null}
            </div>
          ) : (
            <div className="space-y-4">
              {/* recent_2fa is satisfied for a few minutes on this session, so one proof
                  covers the run of admin work someone is usually in the middle of. */}
              <p className="text-sm leading-relaxed text-muted-foreground">
                To {what}, confirm it is you.
              </p>
              {me?.has_passkey && passkeysSupported() && (
                <AuthButton type="button" tone="key" block onClick={confirmWithPasskey} disabled={busy}>
                  Use your passkey
                </AuthButton>
              )}
              {me?.totp_enabled && (
                <div className="space-y-3">
                  <Field label="Authenticator code">
                    <AuthInput
                      code
                      aria-label="Authenticator code"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                    />
                  </Field>
                  <AuthButton
                    type="button"
                    block
                    onClick={confirmWithCode}
                    disabled={code.length < 6 || busy}
                  >
                    Confirm
                  </AuthButton>
                </div>
              )}
              {me?.email_2fa_enabled && (
                <EmailCodeStep
                  email={me.email}
                  sendLabel="Email me a code"
                  verifyLabel="Confirm"
                  send={() => api.request("/api/v1/auth/2fa/email/step-up/send", { method: "POST" })}
                  verify={async (value) => {
                    await api.request("/api/v1/auth/2fa/email/step-up", {
                      method: "POST",
                      json: { code: value },
                    });
                    setDone(true);
                  }}
                />
              )}
              {/* Audit: a passkey-only person on a browser without WebAuthn used to get this
                  dialog with NO button and no explanation - a dead end they could not leave.
                  Say what happened and give them a way out. */}
              {me && !me.totp_enabled && !me.email_2fa_enabled && !(me.has_passkey && passkeysSupported()) && (
                <>
                  <p className="text-sm leading-relaxed text-muted-foreground">
                    {me?.has_passkey
                      ? "This browser can't use passkeys, so we can't confirm it's you here. Open the console in a browser that supports passkeys, or add an authenticator app as a second way in."
                      : "You don't have a way to confirm yet. Add a passkey or an authenticator app first."}
                  </p>
                  <AuthButton
                    type="button"
                    block
                    onClick={() => {
                      setPending(null);
                      window.location.assign("/settings/team?tab=security");
                    }}
                  >
                    Set up a second factor
                  </AuthButton>
                </>
              )}
            </div>
          )}

          {error && (
            <div className="mt-4">
              <AuthAlert>{error}</AuthAlert>
            </div>
          )}

          <div className="mt-5 flex justify-end">
            <button type="button" className="ex-link" onClick={() => setPending(null)}>
              {done ? "Close" : "Cancel"}
            </button>
          </div>
        </AuthPlate>
      </div>
    </div>
  );
}
