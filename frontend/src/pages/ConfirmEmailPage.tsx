import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { mutationErrorMessage } from "@/components/ui/primitives";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Field,
  Lamp,
  StepRail,
} from "@/components/auth/AuthShell";

/** Matches the signup rail, so confirming reads as step two of the same form. */
const STEPS = ["Account", "Confirm email", "Verify identity"];
/** The server refuses a new code within 30s of the last one (services/email_code.py RESEND_AFTER). */
const RESEND_SECONDS = 60;

/** Webmail inboxes worth a one-click shortcut, by address domain. */
const INBOXES: Record<string, { label: string; url: string }> = {
  "gmail.com": { label: "Open Gmail", url: "https://mail.google.com/" },
  "googlemail.com": { label: "Open Gmail", url: "https://mail.google.com/" },
  "outlook.com": { label: "Open Outlook", url: "https://outlook.live.com/mail/0/" },
  "hotmail.com": { label: "Open Outlook", url: "https://outlook.live.com/mail/0/" },
  "live.com": { label: "Open Outlook", url: "https://outlook.live.com/mail/0/" },
  "yahoo.com": { label: "Open Yahoo Mail", url: "https://mail.yahoo.com/" },
  "icloud.com": { label: "Open iCloud Mail", url: "https://www.icloud.com/mail" },
  "proton.me": { label: "Open Proton Mail", url: "https://mail.proton.me/" },
  "protonmail.com": { label: "Open Proton Mail", url: "https://mail.proton.me/" },
};

function inboxFor(email: string | undefined) {
  const domain = email?.split("@")[1]?.toLowerCase();
  return domain ? INBOXES[domain] : undefined;
}

/** An envelope with the Ringlite signal leaving it. Decorative. */
function Envelope({ sealed }: { sealed: boolean }) {
  return (
    <svg viewBox="0 0 96 72" width="96" height="72" aria-hidden="true" className="ex-rise mb-5 block">
      <rect x="6" y="14" width="68" height="50" rx="6" fill="hsl(var(--ex-ink-raise))" stroke="hsl(var(--border))" strokeWidth="1.5" />
      <path d="M8 18 L40 42 L72 18" fill="none" stroke="hsl(var(--primary))" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      {sealed ? (
        <g>
          <circle cx="74" cy="20" r="14" fill="hsl(var(--primary))" />
          <path d="M67 20.5 L72 25.5 L81 15.5" fill="none" stroke="hsl(var(--primary-foreground))" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
        </g>
      ) : (
        <g stroke="hsl(var(--primary))" strokeWidth="2" strokeLinecap="round" fill="none">
          <path d="M80 12 q5 6 0 12" opacity="0.9" />
          <path d="M85 7 q9 11 0 22" opacity="0.55" />
          <path d="M90 2 q13 16 0 32" opacity="0.28" />
        </g>
      )}
    </svg>
  );
}

export function ConfirmEmailPage() {
  const { api, me, refreshMe, logout } = useAuth();
  const [pending, setPending] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [failed, setFailed] = React.useState(false);
  const [done, setDone] = React.useState(false);
  // Registration just sent a code: do not offer a replacement before it can arrive.
  const [wait, setWait] = React.useState(RESEND_SECONDS);
  const [code, setCode] = React.useState("");
  const inbox = inboxFor(me?.email);

  React.useEffect(() => {
    if (wait <= 0) return;
    const t = window.setTimeout(() => setWait((w) => w - 1), 1000);
    return () => window.clearTimeout(t);
  }, [wait]);

  async function confirm() {
    setPending(true);
    setMessage("");
    setFailed(false);
    try {
      await api.request("/api/v1/auth/confirm-email", { method: "POST", json: { code } });
      setDone(true);
      // The next wall (recovery codes) or the app takes over from here.
      await refreshMe();
    } catch (error) {
      setFailed(true);
      setMessage(mutationErrorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function resend() {
    setPending(true);
    setMessage("");
    setFailed(false);
    try {
      await api.request("/api/v1/auth/resend-confirmation", { method: "POST", json: {} });
      setCode("");
      setMessage("Sent. A new code is on its way, and the old one no longer works.");
      setWait(RESEND_SECONDS);
    } catch (error) {
      setFailed(true);
      setMessage(mutationErrorMessage(error));
    } finally {
      setPending(false);
    }
  }

  const state = done ? "done" : "check";

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow={state === "done" ? "Step 02 · Done" : "Step 02 · Confirm email"}
        title={
          state === "done"
            ? "You're confirmed"
            : "Check your inbox"
        }
        lede={
          state === "done" ? (
            "Your email address is confirmed. Next, verify your identity so we can approve your account."
          ) : (
            <>
              We emailed a six-digit code to{" "}
              <b className="font-semibold text-[hsl(var(--ex-bone))] [overflow-wrap:anywhere]">
                {me?.email ?? "your email address"}
              </b>
              . Enter it here to confirm your address.
            </>
          )
        }
        footer={
          me && state !== "done" ? (
            <p className="text-[0.8125rem] text-muted-foreground">
              Wrong address?{" "}
              <button type="button" className="ex-link" onClick={() => void logout()}>
                Sign out and start again
              </button>
            </p>
          ) : null
        }
      >
        <StepRail steps={STEPS} active={state === "done" ? 2 : 1} />
        <Envelope sealed={state === "done"} />

        {state === "check" && (
          <div className="space-y-4">
            {me?.email_confirmation_sent === false && (
              <AuthNotice>
                Your account is ready, but the email did not go out. Send a new code below.
              </AuthNotice>
            )}
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                if (code.length === 6 && !pending) void confirm();
              }}
            >
              <Field label="Email code" hint="Six digits, from the email we just sent.">
                <AuthInput
                  code
                  aria-label="Email code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                />
              </Field>
              <AuthButton type="submit" block disabled={pending || code.length < 6}>
                {pending ? "Checking…" : "Confirm email"}
              </AuthButton>
            </form>
            {inbox && (
              <a className="ex-link block" href={inbox.url} target="_blank" rel="noreferrer">
                {inbox.label}
              </a>
            )}
            <ul className="space-y-1.5 text-[0.8125rem] leading-relaxed text-muted-foreground">
              <li>· It can take up to a minute to arrive. The code works for 10 minutes.</li>
              <li>· Not there after a minute? Check spam or promotions.</li>
            </ul>
            <button
              type="button"
              className="ex-link"
              disabled={pending || wait > 0}
              onClick={() => void resend()}
            >
              {wait > 0 ? `Send a new code in ${wait}s` : "Send a new code"}
            </button>
          </div>
        )}

        {state === "done" && (
          <div className="space-y-4">
            <Lamp state="live">{me?.email ?? "Email"} confirmed</Lamp>
            <AuthButton type="button" block onClick={() => window.location.assign(me ? "/verification" : "/login")}>
              {me ? "Continue to verification" : "Sign in to continue"}
            </AuthButton>
          </div>
        )}

        {message && (
          <div className="mt-4">
            {failed ? <AuthAlert>{message}</AuthAlert> : <Lamp state="live">{message}</Lamp>}
          </div>
        )}
      </AuthPlate>
    </AuthSurface>
  );
}
