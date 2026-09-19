import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthNotice,
  AuthPlate,
  AuthSurface,
  Field,
} from "@/components/auth/AuthShell";

/**
 * P42: request a reset link. The confirmation is deliberately the same whether or not the
 * email has an account - the server answers 202 either way, and this screen must not
 * undo that by looking different. There is one confirmation string and no branch.
 */
export function ForgotPasswordPage() {
  const { api } = useAuth();
  const [email, setEmail] = React.useState("");
  const [sent, setSent] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/password/forgot", { method: "POST", json: { email } });
      setSent(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthSurface>
      <AuthPlate
        as="form"
        onSubmit={onSubmit}
        eyebrow="Recovery · Password"
        title="Reset your password"
        lede={
          sent
            ? undefined
            : "We will send a link to the address on the account. It can be used once."
        }
        footer={
          <Link to="/login" className="ex-link">
            Back to sign in
          </Link>
        }
      >
        {sent ? (
          <AuthNotice>
            If an account exists for that email, a reset link is on its way. It works once and
            expires soon. You will still need your passkey or authenticator app to sign in.
          </AuthNotice>
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
            {error && <AuthAlert>{error}</AuthAlert>}
            <AuthButton type="submit" block disabled={!email || busy}>
              Send reset link
            </AuthButton>
          </div>
        )}
      </AuthPlate>
    </AuthSurface>
  );
}

/**
 * Choosing the new password.
 *
 * NOTE ON THE GUIDANCE COPY: it names no character count, on purpose. The minimum is a
 * deployment setting, so any number hardcoded here is a lie on an installation configured
 * differently - and the policy also refuses anything found in a public breach, so "at
 * least N characters" would promise an acceptance the server will not honour. The rule is
 * described by shape; the server's refusal is rendered verbatim and is the only authority.
 */
export function ResetPasswordPage() {
  const { api } = useAuth();
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("The passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.request("/api/v1/auth/password/reset", {
        method: "POST",
        json: { token, new_password: password },
      });
      setDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthSurface>
      <AuthPlate
        as="form"
        onSubmit={onSubmit}
        eyebrow="Recovery · Password"
        title="Choose a new password"
        lede={
          done || !token
            ? undefined
            : "A long passphrase - three or four unrelated words - beats a short clever one."
        }
        footer={
          <Link to="/login" className="ex-link">
            Back to sign in
          </Link>
        }
      >
        {done ? (
          <AuthNotice>
            Your password was changed and every device was signed out. Sign in with the new
            password and your passkey or authenticator app.
          </AuthNotice>
        ) : !token ? (
          <AuthAlert>This reset link is incomplete.</AuthAlert>
        ) : (
          <div className="space-y-4">
            <Field
              label="New password"
              hint="Passwords that are too short, that look like your email address, or that have appeared in a public breach are refused."
            >
              <AuthInput
                aria-label="New password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <Field label="Confirm new password">
              <AuthInput
                aria-label="Confirm new password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </Field>
            {error && <AuthAlert>{error}</AuthAlert>}
            <AuthButton type="submit" block disabled={!password || busy}>
              Save new password
            </AuthButton>
          </div>
        )}
      </AuthPlate>
    </AuthSurface>
  );
}
