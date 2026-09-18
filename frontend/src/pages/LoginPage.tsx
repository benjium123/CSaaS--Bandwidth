import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { rememberPendingForRecovery } from "@/pages/RecoverAccountPage";
import { passkeysSupported } from "@/lib/webauthn";
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

/**
 * The front door. Two steps: prove who you are, then prove it is still you.
 *
 * THREE RULES THIS FILE OBEYS, all of them the hard way round:
 *
 * 1. A FAILED SIGN-IN READS THE SAME WHATEVER CAUSED IT. Wrong password, locked account
 *    (423), an address with no account at all - the server deliberately answers them
 *    identically so sign-in cannot be used to discover which emails exist, and this page
 *    must not undo that. There is no branch below that picks different failure wording,
 *    and none may be added: the server's message is rendered verbatim, once, in one place.
 *    `code` is read for exactly one thing - offering a next step - never for copy.
 *
 * 2. NO STEP ENDS WITHOUT A WAY ON. Every state of the second-factor step offers at least
 *    one usable route, and every exit keeps `pendingToken` so nothing already proved is
 *    thrown away. That is why "this browser cannot use passkeys" explains itself and
 *    points at the recovery code instead of leaving a button that will only ever fail.
 *
 * 3. A PASSKEY CEREMONY IS NEVER RETRIED IN PLACE. The server burns a challenge on any
 *    outcome, success or failure, so a second attempt with the same challenge id fails as
 *    "expired" and looks like a broken authenticator. `verifyPasskey` asks for fresh
 *    options on every call, so pressing the button again is always a NEW ceremony.
 */
/** Ties the passkey control to the sentence explaining why it cannot be used here. */
const PASSKEY_NOTE_ID = "login-passkey-unsupported";

export function LoginPage() {
  const { login, verify2fa, verifyPasskey, recoverWithCode } = useAuth();
  const navigate = useNavigate();
  const [useRecoveryCode, setUseRecoveryCode] = React.useState(false);
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [code, setCode] = React.useState("");
  const [pendingToken, setPendingToken] = React.useState<string | null>(null);
  const [methods, setMethods] = React.useState<string[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [keyBusy, setKeyBusy] = React.useState(false);
  const [ssoOpen, setSsoOpen] = React.useState(false);
  const [ssoEnforced, setSsoEnforced] = React.useState(false);
  const [orgSlug, setOrgSlug] = React.useState("");

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    const res = pendingToken
      ? useRecoveryCode
        ? await recoverWithCode(pendingToken, code)
        : await verify2fa(pendingToken, code)
      : await login(email, password);
    setBusy(false);

    if (res.kind === "needs_2fa") {
      setPendingToken(res.pendingToken);
      setMethods(res.methods);
      return;
    }
    if (res.kind === "error") {
      setError(res.message);
      // AFFORDANCE ONLY. `sso_required` comes back after a CORRECT password on an account
      // whose workspace enforces single sign-on, so a password will never work here and an
      // error with no way forward is a dead end. Opening the SSO panel gives them the road
      // out. It cannot leak anything: the server only reaches this branch once the password
      // was right. Note what is deliberately NOT here - no lookup that answers "does this
      // address use SSO?" before a password. That endpoint would be an account-enumeration
      // oracle, which is precisely what the rest of this flow is built to prevent.
      if (res.code === "sso_required") {
        setSsoEnforced(true);
        setSsoOpen(true);
      }
    }
  }

  async function onPasskey() {
    if (!pendingToken) return;
    setError(null);
    setKeyBusy(true);
    // Fresh options, fresh challenge, every time - see rule 3 above.
    const res = await verifyPasskey(pendingToken);
    setKeyBusy(false);
    if (res.kind === "error") setError(res.message);
  }

  const secondStep = Boolean(pendingToken);
  const totpAllowed = !secondStep || methods.includes("totp") || useRecoveryCode;
  const passkeyOffered = secondStep && methods.includes("passkey") && !useRecoveryCode;
  // The button stays visible when the browser has no WebAuthn - hiding the only named
  // route would be more confusing than explaining why it cannot be taken - but it is
  // disabled and accompanied by the reason and an alternative.
  const passkeyUsable = passkeyOffered && passkeysSupported();
  // Shown next to the disabled control, not somewhere else on the screen: the sentence is
  // what does the work, the greying is only its echo. It is also a FIXED sentence about
  // this browser - it says nothing about the account, because "compose the copy from what
  // we know about this person" is the habit that breaks the enumeration rule two screens
  // later, even where (as here, past the pending token) that rule is not itself engaged.
  const passkeyUnsupported = passkeyOffered && !passkeysSupported();
  // The remaining silence: the server named no factor this screen can exercise at all -
  // reachable with an empty `methods` list - which used to render a form with nothing on it.
  const noMethodOffered = secondStep && !totpAllowed && !passkeyOffered;

  return (
    <AuthSurface>
      <AuthPlate
        as="form"
        onSubmit={onSubmit}
        eyebrow={secondStep ? "Step 02 · Second factor" : "Step 01 · Identity"}
        title={secondStep ? "Confirm it is you" : "Sign in"}
        lede={
          secondStep
            ? "One more proof, from something you hold rather than something you know."
            : "The console for your numbers, conversations and calls."
        }
        footer={
          secondStep ? (
            <>
              <button
                type="button"
                className="ex-link"
                onClick={() => {
                  setUseRecoveryCode((v) => !v);
                  setCode("");
                  setError(null);
                }}
              >
                {useRecoveryCode ? "Use my passkey or authenticator app" : "Use a recovery code"}
              </button>
              <button
                type="button"
                className="ex-link"
                onClick={() => {
                  // Carries the pending token across, so the recovery flow continues this
                  // sign-in rather than starting a new one.
                  rememberPendingForRecovery(pendingToken as string);
                  navigate("/recover");
                }}
              >
                Lost access to your passkey and authenticator app
              </button>
            </>
          ) : (
            <>
              <Link to="/forgot-password" className="ex-link">
                Forgot your password?
              </Link>
              {/* Only on the first step: once a password has been accepted the account
                  exists, and offering to create one there would be a non-sequitur. */}
              <Link to="/signup" className="ex-link">
                No account? Start a workspace
              </Link>
            </>
          )
        }
      >
        <StepRail steps={["Identity", "Second factor"]} active={secondStep ? 1 : 0} />

        {secondStep ? (
          <div className="space-y-4">
            {passkeyOffered && (
              <div className="space-y-2">
                <AuthButton
                  type="button"
                  tone="key"
                  block
                  // `disabled` only while a request is in flight. The browser-cannot-do-this
                  // case uses `aria-disabled`, so the control keeps its place in the tab
                  // order and a screen reader announces the reason when focus lands on it.
                  disabled={busy || keyBusy}
                  aria-disabled={passkeyUnsupported || undefined}
                  aria-describedby={passkeyUnsupported ? PASSKEY_NOTE_ID : undefined}
                  onClick={() => {
                    if (passkeyUnsupported) return;
                    void onPasskey();
                  }}
                >
                  Use your passkey
                </AuthButton>
                {keyBusy ? (
                  <Lamp state="wait">
                    Waiting for your device. It will ask for your fingerprint, face or PIN.
                  </Lamp>
                ) : null}
                {passkeyUnsupported ? (
                  <div id={PASSKEY_NOTE_ID}>
                    <AuthNotice>
                      This browser cannot use passkeys. Use a recovery code below, or open this
                      page in a browser that supports them - your sign-in is still waiting either
                      way.
                    </AuthNotice>
                  </div>
                ) : null}
              </div>
            )}

            {noMethodOffered ? (
              <AuthNotice>
                This sign-in needs a second factor that is not available on this screen. Use one
                of your recovery codes below, or recover your account.
              </AuthNotice>
            ) : null}

            {passkeyUsable && totpAllowed ? (
              <div className="flex items-center gap-3">
                <hr className="ex-hairline flex-1" />
                <span className="ex-label">or</span>
                <hr className="ex-hairline flex-1 rotate-180" />
              </div>
            ) : null}

            {totpAllowed && (
              <Field
                label={useRecoveryCode ? "Recovery code" : "Authenticator code"}
                hint={
                  useRecoveryCode
                    ? "Each recovery code works once. Using one tells your workspace's admins."
                    : "The six digits currently showing in your authenticator app."
                }
              >
                <AuthInput
                  code
                  aria-label={useRecoveryCode ? "Recovery code" : "Authenticator code"}
                  inputMode={useRecoveryCode ? "text" : "numeric"}
                  autoComplete="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </Field>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            <Field label="Email">
              <AuthInput
                aria-label="Email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <Field label="Password">
              <AuthInput
                aria-label="Password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          </div>
        )}

        {/* One alert, one wording, whatever went wrong. See rule 1. */}
        {error && (
          <div className="mt-4">
            <AuthAlert>{error}</AuthAlert>
          </div>
        )}

        {totpAllowed && (
          <AuthButton type="submit" block disabled={busy || keyBusy} className="mt-5">
            {busy ? "Working..." : secondStep ? "Verify" : "Sign in"}
          </AuthButton>
        )}
      </AuthPlate>

      {!secondStep && (
        <div className="ex-rise mt-6" style={{ ["--d" as string]: "280ms" }}>
          <hr className="ex-hairline mb-4" />
          {!ssoOpen ? (
            <AuthButton type="button" tone="quiet" onClick={() => setSsoOpen(true)}>
              Sign in with SSO
            </AuthButton>
          ) : (
            <div className="space-y-3">
              {/* Shown only when the server itself said this workspace enforces SSO. It
                  restates the server's position; it does not diagnose the account. */}
              {ssoEnforced ? (
                <AuthNotice>
                  This workspace signs in through its own identity provider. Enter its short
                  name to continue there.
                </AuthNotice>
              ) : null}
              <Field
                label="Workspace short name"
                hint="Your workspace's short name, from its sign-in link."
              >
                <AuthInput
                  aria-label="Workspace short name"
                  placeholder="acme"
                  value={orgSlug}
                  onChange={(e) => setOrgSlug(e.target.value)}
                />
              </Field>
              {/* A plain link, not a fetch: the endpoint answers a 302 to the identity
                  provider, so the browser itself has to follow it. Going through the API
                  client would turn the hand-off into a cross-origin XHR and lose the flow. */}
              {orgSlug.trim() ? (
                <a
                  className="ex-btn ex-btn-quiet"
                  href={`/api/v1/auth/sso/${encodeURIComponent(orgSlug.trim())}/start`}
                >
                  Continue
                </a>
              ) : (
                <AuthButton type="button" tone="quiet" disabled>
                  Continue
                </AuthButton>
              )}
            </div>
          )}
        </div>
      )}
    </AuthSurface>
  );
}
