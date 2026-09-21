import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import AdminEntry from '@/pages/AdminEntry';
import AdminMfaPage from '@/pages/AdminMfaPage';

const { mockUseAuth } = vi.hoisted(() => ({
  mockUseAuth: vi.fn(),
}));

vi.mock('@/auth/AuthContext', () => ({
  useAuth: mockUseAuth,
}));

vi.mock('@/pages/OpsPage', () => ({
  OpsPage: () => <div data-testid="ops-page">OpsPage</div>,
}));

vi.mock('@/components/security/StepUpDialog', () => ({
  StepUpDialog: () => <div data-testid="step-up-dialog">StepUpDialog</div>,
}));

vi.mock('@/components/settings/PasskeysCard', () => ({
  PasskeysCard: () => <div data-testid="passkeys-card">PasskeysCard</div>,
}));

vi.mock('@/components/security/TotpEnrolment', () => ({
  TotpEnrolment: () => <div data-testid="totp-enrolment">TotpEnrolment</div>,
}));

vi.mock('@/lib/webauthn', () => ({
  passkeysSupported: () => true,
}));

function makeAuth(overrides: Record<string, unknown> = {}) {
  return {
    me: {
      email: 'admin@example.com',
      is_platform_operator: true,
      operator_role: 'admin',
      second_factor_required: false,
      has_passkey: false,
      passkey_required: true,
      memberships: [],
      totp_enabled: true,
    },
    ready: true,
    api: { request: vi.fn() },
    refreshMe: vi.fn(),
    logout: vi.fn(),
    ...overrides,
  };
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AdminEntry />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseAuth.mockReset();
  mockUseAuth.mockReturnValue(makeAuth());
});

describe('AdminSecurity route', () => {
  it('shows active TOTP status and passkey controls at /admin/security when a factor is already enabled', () => {
    renderAt('/admin/security');

    expect(
      screen.getByText(/authenticator app is active/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /begin enrolment/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('passkeys-card')).toBeInTheDocument();
    expect(screen.getByTestId('step-up-dialog')).toBeInTheDocument();
  });

  it('renders a Back to console link pointing at /admin', () => {
    renderAt('/admin/security');

    const back = screen.getByRole('link', { name: /back to console/i });
    expect(back).toHaveAttribute('href', '/admin');
  });

  it('exposes a Security link on /admin', () => {
    renderAt('/admin');

    const security = screen.getByRole('link', { name: /security/i });
    expect(security).toHaveAttribute('href', '/admin/security');
  });

  it('shows the passkey policy notice with an Add a passkey link when no passkey exists', () => {
    renderAt('/admin');

    expect(
      screen.getByText(/passkey is required for privileged actions/i),
    ).toBeInTheDocument();
    const add = screen.getByRole('link', { name: /add a passkey/i });
    expect(add).toHaveAttribute('href', '/admin/security');
  });

  it('hides the policy notice when a passkey exists but keeps Security available', () => {
    mockUseAuth.mockReturnValue(
      makeAuth({
        me: {
          email: 'admin@example.com',
          is_platform_operator: true,
          operator_role: 'admin',
          second_factor_required: false,
          has_passkey: true,
          passkey_required: true,
          memberships: [],
          totp_enabled: true,
        },
      }),
    );

    renderAt('/admin');

    expect(
      screen.queryByText(/passkey is required for privileged actions/i),
    ).not.toBeInTheDocument();
    const security = screen.getByRole('link', { name: /security/i });
    expect(security).toHaveAttribute('href', '/admin/security');
  });

  it('denies a reviewer-role operator and shows Access denied', () => {
    mockUseAuth.mockReturnValue(
      makeAuth({
        me: {
          email: 'reviewer@example.com',
          is_platform_operator: true,
          operator_role: 'reviewer',
          second_factor_required: false,
          has_passkey: false,
          passkey_required: true,
          memberships: [],
          totp_enabled: true,
        },
      }),
    );

    renderAt('/admin/security');

    expect(
      screen.getByRole('heading', { name: /access denied/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('passkeys-card')).not.toBeInTheDocument();
    expect(screen.queryByTestId('totp-enrolment')).not.toBeInTheDocument();
  });

  it('shows the Administrator sign in gate (not enrollment) when logged out', () => {
    mockUseAuth.mockReturnValue(makeAuth({ me: null }));

    renderAt('/admin/security');

    expect(
      screen.getByRole('heading', { name: /administrator sign in/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('passkeys-card')).not.toBeInTheDocument();
    expect(screen.queryByTestId('totp-enrolment')).not.toBeInTheDocument();
  });
});

describe('AdminMfaPage', () => {
  it('renders a Back to console link pointing at /admin', () => {
    render(
      <MemoryRouter initialEntries={['/admin/security']}>
        <AdminMfaPage />
      </MemoryRouter>,
    );

    const back = screen.getByRole('link', { name: /back to console/i });
    expect(back).toHaveAttribute('href', '/admin');
  });
});
