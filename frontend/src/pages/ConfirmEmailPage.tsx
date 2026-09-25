import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { mutationErrorMessage } from "@/components/ui/primitives";
import {
  AuthAlert,
  AuthButton,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Lamp,
  StepRail,
} from "@/components/auth/AuthShell";

/** Matches the signup rail, so confirming reads as step two of the same form. */
const STEPS = ["Account", "Confirm email", "Verify identity"];
/** The server quietly skips a resend within 60s of the last one (services/email_verification.py). */
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
  const [wait, setWait] = React.useState(0);
  const token = new URLSearchParams(window.location.search).get("token");
  const inbox = inboxFor(me?.email);

  React.useEffect(() => {
    if (wait <= 0) return;
    const t = window.setTimeout(() => setWait((w) => w - 1), 1000);
    return () => window.clearTimeout(t);
  }, [wait]);

  async function act(confirm: boolean) {
    setPending(true);
    setMessage("");
    setFailed(false);
    try {
      await api.request(`/api/v1/auth/${confirm ? "confirm-email" : "resend-confirmation"}`, {
        method: "POST",
        json: confirm ? { token } : {},
      });
      if (confirm) {
        setDone(true);
        window.history.replaceState(null, "", "/confirm-email");
        await refreshMe();
      } else {
        setMessage("Sent. A new link is on its way, and the old one no longer works.");
        setWait(RESEND_SECONDS);
      }
    } catch (error) {
      setFailed(true);
      setMessage(mutationErrorMessage(error));
    } finally {
      setPending(false);
    }
  }

  const state = done ? "done" : token ? "confirm" : "check";

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow={state === "done" ? "Step 02 · Done" : "Step 02 · Confirm email"}
        title={
          state === "done"
            ? "You're confirmed"
            : state === "confirm"
              ? "Confirm your email"
              : "Check your inbox"
        }
        lede={
          state === "done" ? (
            "Your email address is confirmed. Next, verify your identity so we can approve your account."
          ) : state === "confirm" ? (
            "One click and your address is confirmed."
          ) : (
            <>
              We sent a confirmation link to{" "}
              <b className="font-semibold text-[hsl(var(--ex-bone))] [overflow-wrap:anywhere]">
                {me?.email ?? "your email address"}
              </b>
              . Open it on any device to continue.
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
                Your account is ready, but the email did not go out. Send it again below.
              </AuthNotice>
            )}
            {inbox && (
              <a className="ex-btn ex-btn-primary w-full" href={inbox.url} target="_blank" rel="noreferrer">
                {inbox.label}
              </a>
            )}
            <ul className="space-y-1.5 text-[0.8125rem] leading-relaxed text-muted-foreground">
              <li>· The link works for 24 hours.</li>
              <li>· Not there after a minute? Check spam or promotions.</li>
            </ul>
            <AuthButton
              type="button"
              tone={inbox ? "quiet" : "primary"}
              block
              disabled={pending || wait > 0}
              onClick={() => void act(false)}
            >
              {pending ? "Sending…" : wait > 0 ? `Send again in ${wait}s` : "Send the link again"}
            </AuthButton>
          </div>
        )}

        {state === "confirm" && (
          <AuthButton type="button" block disabled={pending} onClick={() => void act(true)}>
            {pending ? "Confirming…" : "Confirm email address"}
          </AuthButton>
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
