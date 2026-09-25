import { StrictMode } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { AdminEntry } from '@/pages/AdminEntry';

type Me = {
  email: string;
  is_platform_operator?: boolean;
  operator_role?: string;
  second_factor_required?: boolean;
  memberships: unknown[];
};

type AuthFixture = {
  me: Me | null;
  ready: boolean;
  request: ReturnType<typeof vi.fn>;
  setAuth: ReturnType<typeof vi.fn>;
  refreshMe: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
  verify2fa: ReturnType<typeof vi.fn>;
  verifyPasskey: ReturnType<typeof vi.fn>;
  recoverWithCode: ReturnType<typeof vi.fn>;
  api: {
    request: ReturnType<typeof vi.fn>;
    setAuth: ReturnType<typeof vi.fn>;
  };
};

const hoisted = vi.hoisted(() => {
  const request = vi.fn();
  const setAuth = vi.fn();
  const fixture: AuthFixture = {
    me: null,
    ready: true,
    request,
    setAuth,
    refreshMe: vi.fn(),
    logout: vi.fn(),
    verify2fa: vi.fn(),
    verifyPasskey: vi.fn(),
    recoverWithCode: vi.fn(),
    api: { request, setAuth },
  };
  return { fixture, support: { value: true } };
});

vi.mock('@/auth/AuthContext', () => ({
  useAuth: () => hoisted.fixture,
}));

vi.mock('@/pages/OpsPage', () => ({
  // The admin entry passes its sign-out, security link and passkey notice INTO the console.
  OpsPage: ({ account, notice }: { account?: React.ReactNode; notice?: React.ReactNode }) => (
    <div data-testid="ops-page">Ops{notice}{account}</div>
  ),
}));

vi.mock('@/components/security/StepUpDialog', () => ({
  StepUpDialog: () => <div data-testid="step-up-dialog" />,
}));

vi.mock('@/components/settings/PasskeysCard', () => ({
  PasskeysCard: ({ onAdded }: { onAdded?: () => void }) => (
    <button type="button" data-testid="passkeys-card" onClick={() => onAdded?.()}>
      Add passkey
    </button>
  ),
}));

vi.mock('@/components/security/TotpEnrolment', () => ({
  TotpEnrolment: ({ secret, uri }: { secret: string; uri: string }) => (
    <div data-testid="totp-enrolment">
      {secret}|{uri}
    </div>
  ),
}));

vi.mock('@/lib/webauthn', () => ({
  passkeysSupported: () => hoisted.support.value,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function resetFixture() {
  hoisted.fixture.me = null;
  hoisted.fixture.ready = true;
  hoisted.fixture.request.mockReset();
  hoisted.fixture.setAuth.mockReset();
  hoisted.fixture.refreshMe.mockReset();
  hoisted.fixture.logout.mockReset();
  hoisted.fixture.verify2fa.mockReset();
  hoisted.fixture.verifyPasskey.mockReset();
  hoisted.fixture.recoverWithCode.mockReset();
  hoisted.fixture.refreshMe.mockResolvedValue({ email: 'admin@example.com', memberships: [] });
  hoisted.support.value = true;
}

function renderEntry(initialPath: string) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[initialPath]}>
        <AdminEntry />
        <LocationProbe />
      </MemoryRouter>
    </StrictMode>,
  );
}

function setUrl(path: string, hash = '') {
  window.history.replaceState({ marker: 'keep-me' }, '', `${path}${hash}`);
}

const adminMe: Me = {
  email: 'admin@example.com',
  is_platform_operator: true,
  operator_role: 'admin',
  memberships: [],
};

const adminMfaMe: Me = {
  ...adminMe,
  second_factor_required: true,
};

const ordinaryMe: Me = {
  email: 'user@example.com',
  memberships: [],
};

const reviewerMe: Me = {
  email: 'reviewer@example.com',
  is_platform_operator: true,
  operator_role: 'reviewer',
  memberships: [],
};

beforeEach(() => {
  resetFixture();
  setUrl('/admin/signup');
});

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('AdminEntry signup invite handling', () => {
  it('shows invitation required with no register control when token missing', () => {
    setUrl('/admin/signup');
    renderEntry('/admin/signup');
    expect(screen.getByRole('heading', { name: /invitation required/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/full name/i)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /administrator sign in/i })).toBeInTheDocument();
  });

  it('ignores query token and shows invitation required', () => {
    setUrl('/admin/signup', '?token=query-token');
    renderEntry('/admin/signup');
    expect(screen.getByRole('heading', { name: /invitation required/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/full name/i)).not.toBeInTheDocument();
  });

  it('strips fragment, preserves history metadata, and writes no storage', async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    setUrl('/admin/signup', '#token=invite-123&email=invitee%40example.com');
    renderEntry('/admin/signup');
    await waitFor(() => {
      expect(window.location.hash).toBe('');
    });
    expect(window.history.state).toEqual({ marker: 'keep-me' });
    expect(setItemSpy).not.toHaveBeenCalled();
    setItemSpy.mockRestore();
  });

  it('submits only personal fields plus invite_token and shows login link on success', async () => {
    const user = userEvent.setup();
    setUrl('/admin/signup', '#token=invite-123&email=invitee%40example.com');
    hoisted.fixture.request.mockResolvedValueOnce({
      id: '1',
      email: 'invitee@example.com',
      full_name: 'Invitee',
      is_platform_operator: true,
      operator_role: 'admin',
      requires_2fa_enrollment: true,
    });
    renderEntry('/admin/signup');

    await user.type(screen.getByLabelText(/full name/i), 'Invitee');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /create administrator account/i }));

    await waitFor(() => {
      expect(hoisted.fixture.request).toHaveBeenCalledTimes(1);
    });
    const call = hoisted.fixture.request.mock.calls[0] as [string, { method?: string; json?: Record<string, unknown> }];
    expect(call[0]).toBe('/api/v1/auth/admin/register');
    expect(call[1].method).toBe('POST');
    expect(call[1].json).toEqual({
      full_name: 'Invitee',
      email: 'invitee@example.com',
      password: 'hunter2hunter2',
      invite_token: 'invite-123',
    });
    expect(await screen.findByRole('heading', { name: /account created/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /administrator sign in/i })).toBeInTheDocument();
  });

  it('shows server error for invalid or expired invitation', async () => {
    const user = userEvent.setup();
    setUrl('/admin/signup', '#token=expired-token');
    hoisted.fixture.request.mockRejectedValueOnce({ code: 'invalid_invite', message: 'Invitation expired.' });
    renderEntry('/admin/signup');

    await user.type(screen.getByLabelText(/full name/i), 'Invitee');
    await user.type(screen.getByLabelText(/email/i), 'invitee@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /create administrator account/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/invitation expired/i);
  });

  it('offers standard login on existing-account conflict and posts to /auth/login', async () => {
    const user = userEvent.setup();
    setUrl('/admin/signup', '#token=invite-123&email=invitee%40example.com');
    hoisted.fixture.request.mockRejectedValueOnce({
      code: 'existing_account_login_required',
      message: 'Account exists.',
    });
    renderEntry('/admin/signup');

    await user.type(screen.getByLabelText(/full name/i), 'Invitee');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /create administrator account/i }));

    expect(await screen.findByText(/sign in with your existing account/i)).toBeInTheDocument();

    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: 'tok',
      requires_2fa: false,
      pending_token: null,
    });
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await waitFor(() => {
      expect(hoisted.fixture.request).toHaveBeenCalledTimes(2);
    });
    const call = hoisted.fixture.request.mock.calls[1] as [string, { method?: string }];
    expect(call[0]).toBe('/api/v1/auth/login');
    expect(call[1].method).toBe('POST');
  });

  it('accepts only on explicit click and preserves token across rerender', async () => {
    const user = userEvent.setup();
    setUrl('/admin/signup', '#token=invite-123');
    hoisted.fixture.me = ordinaryMe;
    hoisted.fixture.request.mockResolvedValueOnce({});
    const view = renderEntry('/admin/signup');

    expect(screen.getByRole('button', { name: /accept invitation/i })).toBeInTheDocument();
    expect(hoisted.fixture.request).not.toHaveBeenCalled();

    view.rerender(
      <StrictMode>
        <MemoryRouter initialEntries={['/admin/signup']}>
          <AdminEntry />
          <LocationProbe />
        </MemoryRouter>
      </StrictMode>,
    );
    expect(screen.getByRole('button', { name: /accept invitation/i })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() => {
      expect(hoisted.fixture.request).toHaveBeenCalledTimes(1);
    });
    const call = hoisted.fixture.request.mock.calls[0] as [string, { json?: Record<string, unknown> }];
    expect(call[0]).toBe('/api/v1/auth/admin/invites/accept');
    expect(call[1].json).toEqual({ invite_token: 'invite-123' });
  });

  it('shows bound email mismatch error and keeps signout available', async () => {
    const user = userEvent.setup();
    setUrl('/admin/signup', '#token=invite-123');
    hoisted.fixture.me = ordinaryMe;
    hoisted.fixture.request.mockRejectedValueOnce({ message: 'Invitation is bound to a different email.' });
    renderEntry('/admin/signup');

    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/bound to a different email/i);
    expect(screen.getByRole('button', { name: /sign out and use a different account/i })).toBeInTheDocument();
  });
});

describe('AdminEntry access control', () => {
  it.each([
    ['ordinary user', ordinaryMe],
    ['reviewer', reviewerMe],
  ])('denies /admin for %s', (_label, me) => {
    hoisted.fixture.me = me;
    renderEntry('/admin');
    expect(screen.getByRole('heading', { name: /access denied/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign out/i })).toBeInTheDocument();
  });

  it.each([
    ['ordinary user', ordinaryMe],
    ['reviewer', reviewerMe],
  ])('denies /admin/login for %s', (_label, me) => {
    hoisted.fixture.me = me;
    renderEntry('/admin/login');
    expect(screen.getByRole('heading', { name: /access denied/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/^password$/i)).not.toBeInTheDocument();
  });

  it('renders ops and step-up for admin without workspace gating', () => {
    hoisted.fixture.me = adminMe;
    renderEntry('/admin');
    expect(screen.getByTestId('ops-page')).toBeInTheDocument();
    expect(screen.getByTestId('step-up-dialog')).toBeInTheDocument();
  });

  it('requires MFA on /admin/login for admin with second factor', () => {
    hoisted.fixture.me = adminMfaMe;
    renderEntry('/admin/login');
    expect(screen.getByRole('heading', { name: /set up two-factor authentication/i })).toBeInTheDocument();
  });

  it('requires MFA on /admin for admin with second factor', () => {
    hoisted.fixture.me = adminMfaMe;
    renderEntry('/admin');
    expect(screen.getByRole('heading', { name: /set up two-factor authentication/i })).toBeInTheDocument();
  });
});

describe('AdminMfaPage enrolment', () => {
  it('enrolls TOTP, activates, refreshes, and rerenders admin on same pathname', async () => {
    const user = userEvent.setup();
    hoisted.fixture.me = adminMfaMe;
    hoisted.fixture.request
      .mockResolvedValueOnce({ secret: 'SECRET', provisioning_uri: 'otpauth://totp/x' })
      .mockResolvedValueOnce({});
    hoisted.fixture.refreshMe.mockImplementation(async () => {
      hoisted.fixture.me = adminMe;
      return adminMe;
    });
    const view = renderEntry('/admin');

    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /begin enrolment/i }));
    expect(await screen.findByTestId('totp-enrolment')).toHaveTextContent('SECRET|otpauth://totp/x');

    await user.type(screen.getByLabelText(/authentication code/i), '123456');
    await user.click(screen.getByRole('button', { name: /^activate$/i }));

    await waitFor(() => {
      expect(hoisted.fixture.request).toHaveBeenCalledTimes(2);
    });
    const enrollCall = hoisted.fixture.request.mock.calls[0] as [string, { json?: Record<string, unknown> }];
    expect(enrollCall[0]).toBe('/api/v1/auth/2fa/enroll');
    expect(enrollCall[1].json).toEqual({ password: 'hunter2hunter2' });
    const activateCall = hoisted.fixture.request.mock.calls[1] as [string, { json?: Record<string, unknown> }];
    expect(activateCall[0]).toBe('/api/v1/auth/2fa/activate');
    expect(activateCall[1].json).toEqual({ code: '123456' });
    expect(hoisted.fixture.refreshMe).toHaveBeenCalled();

    view.rerender(
      <StrictMode>
        <MemoryRouter initialEntries={['/admin']}>
          <AdminEntry />
          <LocationProbe />
        </MemoryRouter>
      </StrictMode>,
    );
    expect(screen.getByTestId('ops-page')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/admin');
  });

  it('refreshes after passkey added', async () => {
    const user = userEvent.setup();
    hoisted.fixture.me = adminMfaMe;
    renderEntry('/admin');
    await user.click(screen.getByTestId('passkeys-card'));
    await waitFor(() => {
      expect(hoisted.fixture.refreshMe).toHaveBeenCalled();
    });
  });
});

describe('AdminLogin factor flows', () => {
  it('posts credentials to the admin login endpoint', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: 'tok',
      requires_2fa: false,
      pending_token: null,
    });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await waitFor(() => {
      expect(hoisted.fixture.request).toHaveBeenCalledTimes(1);
    });
    const call = hoisted.fixture.request.mock.calls[0] as [string, { method?: string; json?: Record<string, unknown> }];
    expect(call[0]).toBe('/api/v1/auth/admin/login');
    expect(call[1].method).toBe('POST');
    expect(call[1].json).toEqual({ email: 'admin@example.com', password: 'hunter2hunter2' });
  });

  it('does not admit on malformed requires_2fa response', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: null,
    });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be started/i);
    expect(screen.queryByTestId('ops-page')).not.toBeInTheDocument();
  });

  it('does not navigate when refreshMe returns null', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: 'tok',
      requires_2fa: false,
      pending_token: null,
    });
    hoisted.fixture.refreshMe.mockResolvedValueOnce(null);
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be loaded/i);
    expect(screen.queryByTestId('ops-page')).not.toBeInTheDocument();
  });

  it('carries pending token to TOTP handler', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: ['totp'],
    });
    hoisted.fixture.verify2fa.mockResolvedValueOnce({ kind: 'ok' });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await user.type(await screen.findByLabelText(/authentication code/i), '123456');
    await user.click(screen.getByRole('button', { name: /^verify$/i }));

    await waitFor(() => {
      expect(hoisted.fixture.verify2fa).toHaveBeenCalledWith('pending-abc', '123456');
    });
  });

  it('carries pending token to passkey handler', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: ['passkey'],
    });
    hoisted.fixture.verifyPasskey.mockResolvedValueOnce({ kind: 'ok' });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await user.click(await screen.findByRole('button', { name: /use a passkey/i }));
    await waitFor(() => {
      expect(hoisted.fixture.verifyPasskey).toHaveBeenCalledWith('pending-abc');
    });
  });

  it('carries pending token to recovery handler', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: ['passkey'],
    });
    hoisted.fixture.recoverWithCode.mockResolvedValueOnce({ kind: 'ok' });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await user.click(await screen.findByRole('button', { name: /use a recovery code/i }));
    await user.type(screen.getByLabelText(/recovery code/i), 'RECOVERY-1');
    await user.click(screen.getByRole('button', { name: /verify recovery code/i }));

    await waitFor(() => {
      expect(hoisted.fixture.recoverWithCode).toHaveBeenCalledWith('pending-abc', 'RECOVERY-1');
    });
  });

  it('shows recovery option when methods list is empty and no guessed TOTP', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: [],
    });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByRole('button', { name: /use a recovery code/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/authentication code/i)).not.toBeInTheDocument();
  });

  it('defaults to TOTP when methods absent', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
    });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByLabelText(/authentication code/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /use a passkey/i })).not.toBeInTheDocument();
  });

  it('shows unsupported passkey notice and keeps recovery available', async () => {
    const user = userEvent.setup();
    hoisted.support.value = false;
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: ['passkey'],
    });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    expect(await screen.findByText(/passkeys are not supported/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /use a recovery code/i })).toBeInTheDocument();
  });

  it('shows server error on failed factor and stays on step, not console', async () => {
    const user = userEvent.setup();
    hoisted.fixture.request.mockResolvedValueOnce({
      access_token: null,
      requires_2fa: true,
      pending_token: 'pending-abc',
      methods: ['totp'],
    });
    hoisted.fixture.verify2fa.mockResolvedValueOnce({ kind: 'error', message: 'Invalid code.' });
    renderEntry('/admin/login');

    await user.type(screen.getByLabelText(/email/i), 'admin@example.com');
    await user.type(screen.getByLabelText(/^password$/i), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: /^sign in$/i }));

    await user.type(await screen.findByLabelText(/authentication code/i), '000000');
    await user.click(screen.getByRole('button', { name: /^verify$/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid code/i);
    expect(screen.queryByTestId('ops-page')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/authentication code/i)).toBeInTheDocument();
  });
});
