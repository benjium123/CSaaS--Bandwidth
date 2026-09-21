import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '@/auth/AuthContext';
import { OpsPage } from '@/pages/OpsPage';
import { StepUpDialog } from '@/components/security/StepUpDialog';
import { AdminMfaPage } from '@/pages/AdminMfaPage';
import { passkeysSupported } from '@/lib/webauthn';

type FactorResult =
  | { kind: 'ok' }
  | { kind: 'error'; message: string }
  | { kind: 'needs_2fa'; pendingToken?: string; methods?: string[] };

type AdminLoginResponse = {
  access_token: string | null;
  requires_2fa: boolean;
  pending_token: string | null;
  methods?: string[];
};

type RegisterResponse = {
  id: string;
  email: string;
  full_name: string;
  is_platform_operator: true;
  operator_role: 'admin';
  requires_2fa_enrollment: true;
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

function errorCode(err: unknown): string | undefined {
  if (isApiError(err) && typeof err.code === 'string') {
    return err.code;
  }
  return undefined;
}

function isAdmin(me: ReturnType<typeof useAuth>['me']): boolean {
  if (!me) return false;
  return me.is_platform_operator === true && me.operator_role === 'admin';
}

function parseInviteFragment(hash: string): { token: string | null; email: string | null } {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!raw) return { token: null, email: null };
  const params = new URLSearchParams(raw);
  const token = params.get('token');
  const email = params.get('email');
  return {
    token: token && token.length > 0 ? token : null,
    email: email && email.length > 0 ? email : null,
  };
}

function BrandHeader({ subtitle }: { subtitle?: string }) {
  return (
    <header className="border-b border-neutral-200 bg-white/80 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/80">
      <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-4 py-4 sm:px-6">
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 items-center justify-center rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
          >
            R
          </span>
          <div className="leading-tight">
            <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">Ringlite</p>
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              {subtitle ?? 'Platform administration'}
            </p>
          </div>
        </div>
      </div>
    </header>
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

function FactorLogin({
  pendingToken,
  methods,
  onSuccess,
  onError,
  submitLabel,
}: {
  pendingToken: string;
  methods?: string[];
  onSuccess: () => void;
  onError: (message: string) => void;
  submitLabel?: string;
}) {
  const { verify2fa, verifyPasskey, recoverWithCode } = useAuth();
  const [code, setCode] = useState('');
  const [recoveryCode, setRecoveryCode] = useState('');
  const [mode, setMode] = useState<'totp' | 'recovery'>('totp');
  const [pending, setPending] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const supportsPasskey = useMemo(() => {
    try {
      return passkeysSupported();
    } catch {
      return false;
    }
  }, []);

  const run = useCallback(
    async (fn: () => Promise<FactorResult>) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setPending(true);
      setLocalError(null);
    onError('');
      try {
        const result = await fn();
        if (result.kind === 'ok') {
          onSuccess();
        } else if (result.kind === 'error') {
          const message = result.message || 'Verification failed.';
          onError(message);
        } else {
          const message = 'Additional verification is still required.';
          onError(message);
        }
      } catch (err) {
        const message = errorMessage(err, 'Verification failed.');
        onError(message);
      } finally {
        inFlight.current = false;
        setPending(false);
      }
    },
    [onSuccess, onError],
  );

  const handleTotp = (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim()) {
      setLocalError('Enter your authentication code.');
      return;
    }
    void run(() => verify2fa(pendingToken, code.trim()));
  };

  const handleRecovery = (e: React.FormEvent) => {
    e.preventDefault();
    if (!recoveryCode.trim()) {
      setLocalError('Enter a recovery code.');
      return;
    }
    void run(() => recoverWithCode(pendingToken, recoveryCode.trim()));
  };

  const handlePasskey = () => {
    void run(() => verifyPasskey(pendingToken));
  };

  const hasMethods = Array.isArray(methods);
  const methodList = hasMethods ? methods : undefined;
const passkeyOffered =
    !!methodList &&
    (methodList.includes('passkey') || methodList.includes('webauthn'));
  const showPasskey = supportsPasskey && passkeyOffered;
  const showTotp = !methodList || methodList.includes('totp') || methodList.includes('authenticator');
  const showRecovery = true;

  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        Two-factor verification is required to continue.
      </p>

      {localError ? <StatusMessage tone="error">{localError}</StatusMessage> : null}

      {showTotp && mode === 'totp' ? (
        <form onSubmit={handleTotp} className="space-y-3" noValidate>
          <Field
            id="admin-2fa-code"
            label="Authentication code"
            value={code}
            onChange={setCode}
            autoComplete="one-time-code"
            required
            disabled={pending}
            hint="Enter the 6-digit code from your authenticator app."
          />
          <SubmitButton pending={pending} pendingLabel="Verifying…">
            {submitLabel ?? 'Verify'}
          </SubmitButton>
        </form>
      ) : null}

      {showRecovery && mode === 'recovery' ? (
        <form onSubmit={handleRecovery} className="space-y-3" noValidate>
          <Field
            id="admin-recovery-code"
            label="Recovery code"
            value={recoveryCode}
            onChange={setRecoveryCode}
            autoComplete="off"
            required
            disabled={pending}
            hint="Use one of your saved recovery codes."
          />
          <SubmitButton pending={pending} pendingLabel="Verifying…">
            Verify recovery code
          </SubmitButton>
        </form>
      ) : null}

      {showPasskey ? (
        <SubmitButton type="button" variant="secondary" pending={pending} pendingLabel="Waiting for passkey…" onClick={handlePasskey}>
          Use a passkey
        </SubmitButton>
      ) : null}

      {passkeyOffered && !supportsPasskey ? (
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          Passkeys are not supported in this browser. Use an authentication code or recovery code instead.
        </p>
      ) : null}

      <div className="flex flex-wrap gap-3 text-xs">
        {showTotp && mode !== 'totp' ? (
          <button
            type="button"
            className="font-medium text-neutral-700 underline underline-offset-2 hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-neutral-100"
            onClick={() => {
              setMode('totp');
              setLocalError(null);
            }}
            disabled={pending}
          >
            Use authentication code
          </button>
        ) : null}
        {showRecovery && mode !== 'recovery' ? (
          <button
            type="button"
            className="font-medium text-neutral-700 underline underline-offset-2 hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-neutral-100"
            onClick={() => {
              setMode('recovery');
              setLocalError(null);
            }}
            disabled={pending}
          >
            Use a recovery code
          </button>
        ) : null}
      </div>

      <p className="text-xs text-neutral-500 dark:text-neutral-400">
        Lost access to all factors? Contact another platform administrator to restore access. Workspace identity recovery does not apply to platform administrators.
      </p>
    </div>
  );
}

function AdminLogin() {
  const { api, refreshMe } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [methods, setMethods] = useState<string[] | undefined>(undefined);
  const inFlight = useRef(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (inFlight.current) return;
    if (!email.trim() || !password) {
      setError('Enter your email and password.');
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      const result = await api.request<AdminLoginResponse>('/api/v1/auth/admin/login', {
        method: 'POST',
        json: { email: email.trim(), password },
      });
      if (result.requires_2fa) {
        if (!result.pending_token) {
          setError('Two-factor verification could not be started. Please try signing in again.');
          return;
        }
        setPendingToken(result.pending_token);
        setMethods(result.methods);
        setPassword('');
        return;
      }
      if (result.access_token) {
        api.setAuth({ token: result.access_token });
      }
      const refreshed = await refreshMe();
      if (!refreshed) {
        setError('Signed in, but your account could not be loaded. Please try signing in again.');
        return;
      }
      navigate('/admin', { replace: true });
    } catch (err) {
      setError(errorMessage(err, 'Unable to sign in.'));
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  const handleFactorSuccess = async () => {
    try {
      const refreshed = await refreshMe();
      if (!refreshed) {
        setError('Signed in, but your account could not be loaded. Please try signing in again.');
        return;
      }
      navigate('/admin', { replace: true });
    } catch (err) {
      setError(errorMessage(err, 'Unable to complete sign in.'));
    }
  };

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
      <BrandHeader subtitle="Administrator sign in" />
      <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
        <div className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Administrator sign in</h1>
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            Access is limited to platform administrators.
          </p>

          <div className="mt-6 space-y-4">
            {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}

            {pendingToken ? (
              <FactorLogin
                pendingToken={pendingToken}
                methods={methods}
                onSuccess={() => {
                  void handleFactorSuccess();
                }}
                onError={(message) => setError(message)}
              />
            ) : (
              <form onSubmit={handleSubmit} className="space-y-4" noValidate>
                <Field
                  id="admin-login-email"
                  label="Email"
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="username"
                  required
                  disabled={pending}
                />
                <Field
                  id="admin-login-password"
                  label="Password"
                  type="password"
                  value={password}
                  onChange={setPassword}
                  autoComplete="current-password"
                  required
                  disabled={pending}
                />
                <SubmitButton pending={pending} pendingLabel="Signing in…">
                  Sign in
                </SubmitButton>
              </form>
            )}

            <p className="text-center text-xs text-neutral-500 dark:text-neutral-400">
              Have an invitation?{' '}
              <Link
                to="/admin/signup"
                className="font-medium text-neutral-700 underline underline-offset-2 hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-neutral-100"
              >
                Accept your invitation
              </Link>
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}

  // __adminInviteHashCapture
function AdminSignup() {
  const { api, me, ready, refreshMe, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [invite, setInvite] = useState(() => parseInviteFragment(window.location.hash));
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState(invite.email ?? '');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState(false);
  const [existingAccount, setExistingAccount] = useState(false);
  const [existingPendingToken, setExistingPendingToken] = useState<string | null>(null);
  const [existingMethods, setExistingMethods] = useState<string[] | undefined>(undefined);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const acceptInFlight = useRef(false);

    useEffect(() => {
    const captureInviteFromHash = () => {
      const parsed = parseInviteFragment(window.location.hash);
      if (!parsed || !parsed.token) {
        return;
      }
      setInvite(parsed);
      setEmail(parsed.email ?? '');
      setPassword('');
      setFullName('');
      setError('');
      setAcceptError('');
      setCreated(false);
      setExistingAccount(false);
      setExistingPendingToken(null);
      setExistingMethods([]);
      const { pathname, search } = window.location;
      window.history.replaceState(
        window.history.state,
        '',
        `${pathname}${search}`,
      );
    };

    captureInviteFromHash();
    window.addEventListener('hashchange', captureInviteFromHash);
    return () => {
      window.removeEventListener('hashchange', captureInviteFromHash);
    };
  }, [location.hash]);


  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    if (inFlight.current) return;
    if (!invite.token) return;
    if (!fullName.trim() || !email.trim() || !password) {
      setError('Complete all fields to continue.');
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      await api.request<RegisterResponse>('/api/v1/auth/admin/register', {
        method: 'POST',
        json: {
          full_name: fullName.trim(),
          email: email.trim(),
          password,
          invite_token: invite.token,
        },
      });
      setInvite((prev) => ({ ...prev, token: null }));
      setPassword('');
      setCreated(true);
    } catch (err) {
      const code = errorCode(err);
      if (code === 'existing_account_login_required') {
        setExistingAccount(true);
        setPassword('');
        setError(errorMessage(err, 'An account already exists for this email. Sign in to accept the invitation.'));
      } else {
        setError(errorMessage(err, 'Unable to create your account.'));
      }
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  const handleExistingLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (inFlight.current) return;
    if (!email.trim() || !password) {
      setError('Enter your email and password.');
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      const result = await api.request<AdminLoginResponse>('/api/v1/auth/login', {
        method: 'POST',
        json: { email: email.trim(), password },
      });
      if (result.requires_2fa) {
        if (!result.pending_token) {
          setError('Two-factor verification could not be started. Please try signing in again.');
          return;
        }
        setExistingPendingToken(result.pending_token);
        setExistingMethods(result.methods);
        setPassword('');
        return;
      }
      if (result.access_token) {
        api.setAuth({ token: result.access_token });
      }
      const refreshed = await refreshMe();
      if (!refreshed) {
        setError('Signed in, but your account could not be loaded. Please try signing in again.');
      }
    } catch (err) {
      setError(errorMessage(err, 'Unable to sign in.'));
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  const handleExistingFactorSuccess = async () => {
    try {
      const refreshed = await refreshMe();
      if (!refreshed) {
        setError('Signed in, but your account could not be loaded. Please try signing in again.');
      }
    } catch (err) {
      setError(errorMessage(err, 'Unable to complete sign in.'));
    }
  };

  const handleAccept = async () => {
    if (!invite.token || acceptInFlight.current) return;
    acceptInFlight.current = true;
    setAccepting(true);
    setAcceptError(null);
    try {
      await api.request<unknown>('/api/v1/auth/admin/invites/accept', {
        method: 'POST',
        json: { invite_token: invite.token },
      });
      const refreshed = await refreshMe();
      if (!refreshed) {
        setAcceptError('Invitation accepted, but your account could not be loaded. Please sign in again.');
        return;
      }
      setInvite((prev) => ({ ...prev, token: null }));
      navigate('/admin', { replace: true });
    } catch (err) {
      setAcceptError(errorMessage(err, 'Unable to accept the invitation.'));
    } finally {
      acceptInFlight.current = false;
      setAccepting(false);
    }
  };

  const handleSignOut = () => {
    logout();
  };

  if (!ready) {
    return (
      <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
        <BrandHeader subtitle="Administrator invitation" />
        <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
          <p role="status" className="text-sm text-neutral-500 dark:text-neutral-400">
            Loading…
          </p>
        </main>
      </div>
    );
  }

  if (!invite.token && !created) {
    return (
      <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
        <BrandHeader subtitle="Administrator invitation" />
        <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
          <div className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
            <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Invitation required</h1>
            <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
              Administrator accounts are created by invitation only. Open the invitation link you received to continue.
            </p>
            <div className="mt-6 space-y-3">
              <Link
                to="/admin/login"
                className="inline-flex w-full items-center justify-center rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-800 focus:outline-none focus:ring-2 focus:ring-neutral-900 focus:ring-offset-2 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200 dark:focus:ring-neutral-100 dark:focus:ring-offset-neutral-950"
              >
                Go to administrator sign in
              </Link>
              <Link
                to="/login"
                className="block text-center text-xs font-medium text-neutral-600 underline underline-offset-2 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
              >
                Customer sign in
              </Link>
            </div>
          </div>
        </main>
      </div>
    );
  }

  if (created) {
    return (
      <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
        <BrandHeader subtitle="Administrator invitation" />
        <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
          <div className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
            <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Account created</h1>
            <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
              Your administrator account has been created. Sign in to continue.
            </p>
            <div className="mt-6">
              <Link
                to="/admin/login"
                className="inline-flex w-full items-center justify-center rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-800 focus:outline-none focus:ring-2 focus:ring-neutral-900 focus:ring-offset-2 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200 dark:focus:ring-neutral-100 dark:focus:ring-offset-neutral-950"
              >
                Go to administrator sign in
              </Link>
            </div>
          </div>
        </main>
      </div>
    );
  }

  const signedIn = me !== null;

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
      <BrandHeader subtitle="Administrator invitation" />
      <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
        <div className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Accept administrator invitation</h1>
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            {signedIn
              ? 'Confirm that you want to accept this invitation for the signed-in account.'
              : 'Create your administrator account to accept the invitation.'}
          </p>

          <div className="mt-6 space-y-4">
            {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
            {acceptError ? <StatusMessage tone="error">{acceptError}</StatusMessage> : null}

            {signedIn ? (
              <div className="space-y-4">
                <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm dark:border-neutral-800 dark:bg-neutral-950">
                  <p className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">Signed in as</p>
                  <p className="font-medium text-neutral-900 dark:text-neutral-100">{me?.email}</p>
                </div>
                <SubmitButton type="button" pending={accepting} pendingLabel="Accepting…" onClick={() => void handleAccept()}>
                  Accept invitation
                </SubmitButton>
                <button
                  type="button"
                  onClick={handleSignOut}
                  className="w-full text-center text-xs font-medium text-neutral-600 underline underline-offset-2 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
                >
                  Sign out and use a different account
                </button>
              </div>
            ) : existingAccount ? (
              <div className="space-y-4">
                <p className="text-sm text-neutral-600 dark:text-neutral-400">
                  Sign in with your existing account to accept the invitation.
                </p>
                {existingPendingToken ? (
                  <FactorLogin
                    pendingToken={existingPendingToken}
                    methods={existingMethods}
                    onSuccess={() => {
                      void handleExistingFactorSuccess();
                    }}
                    onError={(message) => setError(message)}
                  />
                ) : (
                  <form onSubmit={handleExistingLogin} className="space-y-4" noValidate>
                    <Field
                      id="admin-invite-login-email"
                      label="Email"
                      type="email"
                      value={email}
                      onChange={setEmail}
                      autoComplete="username"
                      required
                      disabled={pending}
                    />
                    <Field
                      id="admin-invite-login-password"
                      label="Password"
                      type="password"
                      value={password}
                      onChange={setPassword}
                      autoComplete="current-password"
                      required
                      disabled={pending}
                    />
                    <SubmitButton pending={pending} pendingLabel="Signing in…">
                      Sign in
                    </SubmitButton>
                  </form>
                )}
              </div>
            ) : (
              <form onSubmit={handleRegister} className="space-y-4" noValidate>
                <Field
                  id="admin-signup-name"
                  label="Full name"
                  value={fullName}
                  onChange={setFullName}
                  autoComplete="name"
                  required
                  disabled={pending}
                />
                <Field
                  id="admin-signup-email"
                  label="Email"
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="email"
                  required
                  disabled={pending}
                  hint="Use the email address this invitation was sent to."
                />
                <Field
                  id="admin-signup-password"
                  label="Password"
                  type="password"
                  value={password}
                  onChange={setPassword}
                  autoComplete="new-password"
                  required
                  disabled={pending}
                />
                <SubmitButton pending={pending} pendingLabel="Creating account…">
                  Create administrator account
                </SubmitButton>
              </form>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function AccessDenied() {
  const { logout } = useAuth();
  const [pending, setPending] = useState(false);

  const handleSignOut = () => {
    if (pending) return;
    setPending(true);
    logout();
  };

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
      <BrandHeader subtitle="Access denied" />
      <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
        <div className="rounded-xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Access denied</h1>
          <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
            This account does not have platform administrator access.
          </p>
          <div className="mt-6">
            <SubmitButton type="button" pending={pending} pendingLabel="Signing out…" onClick={handleSignOut}>
              Sign out
            </SubmitButton>
          </div>
        </div>
      </main>
    </div>
  );
}

function AdminConsole() {
  const { me, logout } = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  const handleSignOut = () => {
    if (signingOut) return;
    setSigningOut(true);
    logout();
  };

  return (
    <div className="console-surface is-light min-h-screen bg-background text-foreground">
      <header className="border-b border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="flex h-9 w-9 items-center justify-center rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
            >
              R
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">Ringlite</p>
              <p className="text-xs text-neutral-500 dark:text-neutral-400">Platform administration</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {me?.email ? (
              <span className="hidden text-sm text-neutral-600 sm:inline dark:text-neutral-400">{me.email}</span>
            ) : null}
                  <Link to="/admin/security" className="text-sm underline">
        Security
      </Link>
<button
              type="button"
              onClick={handleSignOut}
              disabled={signingOut}
              className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-800 focus:outline-none focus:ring-2 focus:ring-neutral-900 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200 dark:focus:ring-neutral-100 dark:focus:ring-offset-neutral-950"
            >
              {signingOut ? 'Signing out…' : 'Sign out'}
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
      {me?.passkey_required && !me.has_passkey && (
        <div className="mb-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          A passkey is required for privileged actions.{" "}
          <Link to="/admin/security" className="underline">
            Add a passkey
          </Link>
        </div>
      )}
        <OpsPage />
      </main>
      <StepUpDialog />
    </div>
  );
}

export function AdminEntry() {
  const { me, ready } = useAuth();
  const location = useLocation();

  const path = location.pathname;
  const isSignup = path === '/admin/signup';
  const isLogin = path === '/admin/login';

  if (isSignup) {
    return <AdminSignup />;
  }

  if (!ready) {
    return (
      <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
        <BrandHeader />
        <main className="mx-auto w-full max-w-md px-4 py-10 sm:px-6">
          <p role="status" className="text-sm text-neutral-500 dark:text-neutral-400">
            Loading…
          </p>
        </main>
      </div>
    );
  }

  if (isLogin) {
    if (!me) {
      return <AdminLogin />;
    }
    if (!isAdmin(me)) {
      return <AccessDenied />;
    }
    if (me.second_factor_required) {
      return (
      <>
        <AdminMfaPage />
        <StepUpDialog />
      </>
    );
    }
    return <AdminConsole />;
  }

  if (!me) {
    return <AdminLogin />;
  }

  if (!isAdmin(me)) {
    return <AccessDenied />;
  }

  if ((me.second_factor_required || path === '/admin/security')) {
    return (
      <>
        <AdminMfaPage />
        <StepUpDialog />
      </>
    );
  }

  return <AdminConsole />;
}

export default AdminEntry;
