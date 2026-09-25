import { StrictMode } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BrowserRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AdminEntry } from './AdminEntry';

const { request, setAuth, refreshMe, logout } = vi.hoisted(() => ({
  request: vi.fn(),
  setAuth: vi.fn(),
  refreshMe: vi.fn(),
  logout: vi.fn(),
}));

vi.mock('@/auth/AuthContext', () => ({
  useAuth: () => ({
    ready: true,
    me: null,
    api: { request, setAuth },
    refreshMe,
    logout,
  }),
}));

vi.mock('@/pages/OpsPage', () => ({
  // The admin entry passes its sign-out, security link and passkey notice INTO the console.
  OpsPage: ({ account, notice }: { account?: React.ReactNode; notice?: React.ReactNode }) => (
    <div data-testid="ops-page">{notice}{account}</div>
  ),
}));

vi.mock('@/components/security/StepUpDialog', () => ({
  StepUpDialog: () => null,
}));

vi.mock('@/pages/AdminMfaPage', () => ({
  AdminMfaPage: () => null,
}));

vi.mock('@/lib/webauthn', () => ({
  passkeysSupported: () => false,
}));

function renderAdminEntry() {
  return render(
    <StrictMode>
      <BrowserRouter>
        <AdminEntry />
      </BrowserRouter>
    </StrictMode>,
  );
}

function applyHash(hash: string) {
  act(() => {
    window.history.replaceState(
      window.history.state,
      '',
      `${window.location.pathname}${window.location.search}${hash}`,
    );
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  });
}

beforeEach(() => {
  request.mockReset();
  setAuth.mockReset();
  refreshMe.mockReset();
  logout.mockReset();

  window.history.replaceState(
    { preserved: 'metadata' },
    '',
    '/admin/signup',
  );
});

describe('AdminSignup invite navigation', () => {
  it('captures a hash-only invite token, prefills email, and erases the fragment', async () => {
    renderAdminEntry();

    expect(
      await screen.findByText(/invitation required/i),
    ).toBeInTheDocument();

    applyHash('#token=invite-token-1&email=invitee%40example.com');

    const fullName = await screen.findByLabelText(/full name/i);
    const email = await screen.findByLabelText(/email/i);

    expect(email).toHaveValue('invitee@example.com');
    expect(window.location.hash).toBe('');
    expect(window.history.state).toEqual(
      expect.objectContaining({ preserved: 'metadata' }),
    );
    expect(window.location.pathname).toBe('/admin/signup');

    request.mockResolvedValueOnce({ ok: true });

    const user = userEvent.setup();
    await user.type(fullName, 'Invitee Person');
    await user.type(await screen.findByLabelText(/password/i), 'sup3r-secret');
    await user.click(
      screen.getByRole('button', {
        name: 'Create administrator account',
      }),
    );

    await waitFor(() => {
      expect(request).toHaveBeenCalled();
    });

    const registerCall = request.mock.calls.find(([path]) =>
      String(path).includes('/api/v1/auth/admin/register'),
    );
    expect(registerCall).toBeTruthy();

    const [, options] = registerCall as [string, { json?: Record<string, unknown> }];
    expect(options?.json).toEqual({
      invite_token: 'invite-token-1',
      email: 'invitee@example.com',
      full_name: 'Invitee Person',
      password: 'sup3r-secret',
    });
  });

  it('keeps a captured token across empty-fragment rerenders and sends the second token after a new hash', async () => {
    window.history.replaceState(
      { preserved: 'metadata' },
      '',
      '/admin/signup#token=invite-token-1&email=first%40example.com',
    );

    const { rerender } = render(
      <StrictMode>
        <BrowserRouter>
          <AdminEntry />
        </BrowserRouter>
      </StrictMode>,
    );

    const email = await screen.findByLabelText(/email/i);
    expect(email).toHaveValue('first@example.com');
    expect(window.location.hash).toBe('');

    rerender(
      <StrictMode>
        <BrowserRouter>
          <AdminEntry />
        </BrowserRouter>
      </StrictMode>,
    );

    expect(await screen.findByLabelText(/email/i)).toHaveValue(
      'first@example.com',
    );

    applyHash('#token=invite-token-2&email=second%40example.com');

    await waitFor(() => {
      expect(screen.getByLabelText(/email/i)).toHaveValue(
        'second@example.com',
      );
    });

    request.mockResolvedValueOnce({ ok: true });

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/full name/i), 'Second Person');
    await user.type(await screen.findByLabelText(/password/i), 'sup3r-secret');
    await user.click(
      screen.getByRole('button', {
        name: 'Create administrator account',
      }),
    );

    await waitFor(() => {
      expect(request).toHaveBeenCalled();
    });

    const registerCall = request.mock.calls.find(([path]) =>
      String(path).includes('/api/v1/auth/admin/register'),
    );
    expect(registerCall).toBeTruthy();

    const [, options] = registerCall as [string, { json?: Record<string, unknown> }];
    expect(options?.json).toEqual({
      invite_token: 'invite-token-2',
      email: 'second@example.com',
      full_name: 'Second Person',
      password: 'sup3r-secret',
    });
  });
});
