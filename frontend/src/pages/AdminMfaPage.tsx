import React, { useCallback, useRef, useState } from 'react';
import { useAuth } from '@/auth/AuthContext';
import { PasskeysCard } from '@/components/settings/PasskeysCard';
import { TotpEnrolment } from '@/components/security/TotpEnrolment';

type EnrollResponse = {
  secret: string;
  provisioning_uri: string;
};

type ApiError = {
  code?: string;
  message?: string;
};

function isApiError(value: unknown): value is ApiError {
  return typeof value === 'object' && value !== null;
}

function errorMessage(err: unknown, fallback: string): string {
  if (isApiError(err) && typeof err.message === 'string' && err.message.length > 0) {
    return err.message;
  }
  if (err instanceof Error && err.message) {
    return err.message;
  }
  return fallback;
}

function StatusMessage({ tone, children }: { tone: 'error' | 'info' | 'success'; children: React.ReactNode }) {
  const toneClass =
    tone === 'error'
      ? 'border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200'
      : tone === 'success'
        ? 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200'
        : 'border-neutral-300 bg-neutral-50 text-neutral-700 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300';
  return (
    <div role={tone === 'error' ? 'alert' : 'status'} className={`rounded-md border px-3 py-2 text-sm ${toneClass}`}>
      {children}
    </div>
  );
}

function Field({
  id,
  label,
  type = 'text',
  value,
  onChange,
  autoComplete,
  required,
  hint,
  disabled,
}: {
  id: string;
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete?: string;
  required?: boolean;
  hint?: string;
  disabled?: boolean;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-neutral-800 dark:text-neutral-200">
        {label}
      </label>
      <input
        id={id}
        name={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        required={required}
        disabled={disabled}
        aria-describedby={hintId}
        className="block w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 shadow-sm outline-none transition focus:border-neutral-900 focus:ring-2 focus:ring-neutral-900/20 disabled:cursor-not-allowed disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100 dark:focus:border-neutral-100 dark:focus:ring-neutral-100/20"
      />
      {hint ? (
        <p id={hintId} className="text-xs text-neutral-500 dark:text-neutral-400">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function SubmitButton({
  children,
  pending,
  pendingLabel,
  type = 'submit',
  onClick,
  variant = 'primary',
}: {
  children: React.ReactNode;
  pending?: boolean;
  pendingLabel?: string;
  type?: 'submit' | 'button';
  onClick?: () => void;
  variant?: 'primary' | 'secondary';
}) {
  const base =
    'inline-flex w-full items-center justify-center rounded-md px-4 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:focus:ring-offset-neutral-950';
  const styles =
    variant === 'primary'
      ? 'bg-neutral-900 text-white hover:bg-neutral-800 focus:ring-neutral-900 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200 dark:focus:ring-neutral-100'
      : 'border border-neutral-300 bg-white text-neutral-800 hover:bg-neutral-50 focus:ring-neutral-400 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200 dark:hover:bg-neutral-800';
  return (
    <button type={type} onClick={onClick} disabled={pending} className={`${base} ${styles}`}>
      {pending ? pendingLabel ?? 'Working…' : children}
    </button>
  );
}

export function AdminMfaPage() {
  const { me, api, refreshMe, logout } = useAuth();
  const [password, setPassword] = useState('');
  const [enrolling, setEnrolling] = useState(false);
  const [enrollError, setEnrollError] = useState<string | null>(null);
  const [enrolment, setEnrolment] = useState<EnrollResponse | null>(null);
  const [code, setCode] = useState('');
  const [activating, setActivating] = useState(false);
  const [activateError, setActivateError] = useState<string | null>(null);
  const [activated, setActivated] = useState(false);
  const enrollInFlight = useRef(false);
  const activateInFlight = useRef(false);

  const handleEnroll = async (e: React.FormEvent) => {
    e.preventDefault();
    if (enrollInFlight.current) return;
    if (!password) {
      setEnrollError('Enter your password to begin enrolment.');
      return;
    }
    enrollInFlight.current = true;
    setEnrolling(true);
    setEnrollError(null);
    try {
      const result = await api.request<EnrollResponse>('/api/v1/auth/2fa/enroll', {
        method: 'POST',
        json: { password },
      });
      setEnrolment({ secret: result.secret, provisioning_uri: result.provisioning_uri });
      setPassword('');
    } catch (err) {
      setEnrollError(errorMessage(err, 'Unable to start enrolment.'));
    } finally {
      enrollInFlight.current = false;
      setEnrolling(false);
    }
  };

  const handleActivate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (activateInFlight.current) return;
    if (!code.trim()) {
      setActivateError('Enter the code from your authenticator app.');
      return;
    }
    activateInFlight.current = true;
    setActivating(true);
    setActivateError(null);
    try {
      await api.request<unknown>('/api/v1/auth/2fa/activate', {
        method: 'POST',
        json: { code: code.trim() },
      });
      await refreshMe();
      setActivated(true);
    } catch (err) {
      setActivateError(errorMessage(err, 'Unable to activate two-factor authentication.'));
    } finally {
      activateInFlight.current = false;
      setActivating(false);
    }
  };

  const handlePasskeyAdded = useCallback(async () => {
    try {
      await refreshMe();
    } catch {
      // refreshMe failure is non-fatal here; backend remains authority.
    }
  }, [refreshMe]);

  const handleSignOut = () => {
    logout();
  };

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
      <header className="border-b border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mx-auto flex w-full max-w-3xl items-center justify-between px-4 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="flex h-9 w-9 items-center justify-center rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
            >
              O
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">OrvoIP</p>
              <p className="text-xs text-neutral-500 dark:text-neutral-400">Administrator security</p>
            </div>
          </div>
                <a href="/admin" className="text-sm underline">
        Back to console
      </a>
<button
            type="button"
            onClick={handleSignOut}
            className="rounded-md border border-neutral-300 bg-white px-3 py-1.5 text-sm font-medium text-neutral-800 transition hover:bg-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200 dark:hover:bg-neutral-800"
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
        <div className="space-y-6">
          <div>
            <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
              Set up two-factor authentication
            </h1>
            <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
              Administrator accounts require a second factor. Add an authenticator app or a passkey to continue.
            </p>
          </div>

          {activated ? (
            <StatusMessage tone="success">
              Two-factor authentication is active. You can continue once the page refreshes.
            </StatusMessage>
          ) : null}

          <section className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
            <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Authenticator app</h2>
            <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
              Manage authenticator protection for your administrator account.
            </p>

            <div className="mt-4 space-y-4">
              {enrollError ? <StatusMessage tone="error">{enrollError}</StatusMessage> : null}
              {activateError ? <StatusMessage tone="error">{activateError}</StatusMessage> : null}

              {me?.totp_enabled ? (
        <p className="text-sm text-green-700">
          Authenticator app is active.
        </p>
      ) : !enrolment ? (
                <form onSubmit={handleEnroll} className="space-y-4" noValidate>
                  <Field
                    id="admin-mfa-password"
                    label="Password"
                    type="password"
                    value={password}
                    onChange={setPassword}
                    autoComplete="current-password"
                    required
                    disabled={enrolling}
                  />
                  <SubmitButton pending={enrolling} pendingLabel="Starting…">
                    Begin enrolment
                  </SubmitButton>
                </form>
              ) : (
                <div className="space-y-4">
                  <TotpEnrolment secret={enrolment.secret} uri={enrolment.provisioning_uri} />
                  <form onSubmit={handleActivate} className="space-y-4" noValidate>
                    <Field
                      id="admin-mfa-code"
                      label="Authentication code"
                      value={code}
                      onChange={setCode}
                      autoComplete="one-time-code"
                      required
                      disabled={activating}
                      hint="Enter the 6-digit code from your authenticator app."
                    />
                    <SubmitButton pending={activating} pendingLabel="Activating…">
                      Activate
                    </SubmitButton>
                  </form>
                </div>
              )}
            </div>
          </section>

          <section className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
            <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Passkey</h2>
            <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
              Register a passkey as an additional factor for this administrator account.
            </p>
            <div className="mt-4">
              <PasskeysCard onAdded={handlePasskeyAdded} />
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}

export default AdminMfaPage;
