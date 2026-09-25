import * as React from "react";
import { AuthAlert, AuthButton, AuthInput, Field, Lamp } from "@/components/auth/AuthShell";

/** Matches services/email_code.RESEND_AFTER on the server. */
const RESEND_SECONDS = 30;

/**
 * "We emailed you a code": send, type six digits, confirm. Used for signing in, for turning
 * email codes on, and for step-up. `send` and `verify` are the only things that differ.
 */
export function EmailCodeStep({
  email,
  send,
  verify,
  sendLabel = "Email me a code",
  verifyLabel = "Verify",
  autoSend = false,
  disabled = false,
}: {
  email?: string | null;
  send: () => Promise<unknown>;
  verify: (code: string) => Promise<unknown>;
  sendLabel?: string;
  verifyLabel?: string;
  /** Send as soon as this appears (sign-in, where the code is the only way forward). */
  autoSend?: boolean;
  disabled?: boolean;
}) {
  const [sent, setSent] = React.useState(false);
  const [code, setCode] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [wait, setWait] = React.useState(0);
  const autoSent = React.useRef(false);

  React.useEffect(() => {
    if (wait <= 0) return;
    const t = window.setTimeout(() => setWait((w) => w - 1), 1000);
    return () => window.clearTimeout(t);
  }, [wait]);

  const doSend = React.useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      await send();
      setSent(true);
      setWait(RESEND_SECONDS);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }, [send]);

  React.useEffect(() => {
    if (autoSend && !autoSent.current) {
      autoSent.current = true;
      void doSend();
    }
  }, [autoSend, doSend]);

  async function doVerify() {
    setError(null);
    setBusy(true);
    try {
      await verify(code.trim());
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  if (!sent) {
    return (
      <div className="space-y-3">
        {busy ? <Lamp state="wait">Sending your code…</Lamp> : null}
        {error && <AuthAlert>{error}</AuthAlert>}
        {!busy && (
          <AuthButton type="button" block onClick={() => void doSend()} disabled={disabled}>
            {sendLabel}
          </AuthButton>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <Lamp state="live">
        Code sent{email ? <> to {email}</> : null}. It works for 10 minutes.
      </Lamp>
      <Field label="Email code" hint="Six digits, from the email we just sent. Check spam if it is not there.">
        <AuthInput
          code
          aria-label="Email code"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              if (code.length === 6 && !busy) void doVerify();
            }
          }}
        />
      </Field>
      {error && <AuthAlert>{error}</AuthAlert>}
      <AuthButton type="button" block onClick={() => void doVerify()} disabled={code.length !== 6 || busy || disabled}>
        {busy ? "Checking…" : verifyLabel}
      </AuthButton>
      <button
        type="button"
        className="ex-link"
        disabled={wait > 0 || busy}
        onClick={() => {
          setCode("");
          void doSend();
        }}
      >
        {wait > 0 ? `Send a new code in ${wait}s` : "Send a new code"}
      </button>
    </div>
  );
}
