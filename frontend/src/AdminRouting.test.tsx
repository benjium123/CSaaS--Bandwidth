import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { App } from "@/App";
import { useAuth } from "@/auth/AuthContext";

vi.mock("@/auth/AuthContext", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/pages/AdminEntry", () => {
  const AdminEntry = () => <div data-testid="admin-entry" />;
  return { default: AdminEntry, AdminEntry };
});

vi.mock("@/pages/LoginPage", () => ({
  LoginPage: () => <div data-testid="login-page" />,
}));

vi.mock("@/pages/SignUpPage", () => ({
  SignUpPage: () => <div data-testid="signup-page" />,
}));

vi.mock("@/pages/OpsPage", () => ({
  OpsPage: () => <div data-testid="ops-page" />,
}));

vi.mock("@/pages/OrgPickerPage", () => ({
  OrgPickerPage: () => <div data-testid="org-picker-page" />,
}));

vi.mock("@/pages/SecureAccountPage", () => ({
  SecureAccountPage: () => <div data-testid="secure-account-page" />,
}));

vi.mock("@/components/security/StepUpDialog", () => ({
  StepUpDialog: () => <div data-testid="step-up-dialog" />,
}));

const mockedUseAuth = vi.mocked(useAuth);

interface Membership {
  org_id: string;
  role: string;
}

interface AuthUser {
  id: string;
  email: string;
  full_name: string;
  memberships: Membership[];
  is_platform_operator?: boolean;
  operator_role?: string;
  second_factor_required?: boolean;
}

interface AuthFixture {
  me: AuthUser | null;
  orgId: string | null;
  ready: boolean;
}

const baseFixture: AuthFixture = {
  me: null,
  orgId: null,
  ready: true,
};

function setAuthFixture(overrides: Partial<AuthFixture> = {}): void {
  mockedUseAuth.mockReturnValue({
    ...baseFixture,
    ...overrides,
  } as ReturnType<typeof useAuth>);
}

function renderAt(path: string): void {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App admin routing", () => {
  beforeEach(() => {
    setAuthFixture();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("routes unauthenticated /admin/login to admin entry, not customer login", () => {
    setAuthFixture({ me: null, orgId: null, ready: true });
    renderAt("/admin/login");

    expect(screen.getByTestId("admin-entry")).toBeTruthy();
    expect(screen.queryByTestId("login-page")).toBeNull();
  });

  it("routes unauthenticated /admin/signup to admin entry, not customer signup", () => {
    setAuthFixture({ me: null, orgId: null, ready: true });
    renderAt("/admin/signup");

    expect(screen.getByTestId("admin-entry")).toBeTruthy();
    expect(screen.queryByTestId("signup-page")).toBeNull();
  });

  it("routes orgless admin /admin to admin entry, not org picker", () => {
    setAuthFixture({
      me: {
        id: "admin-1",
        email: "admin@example.test",
        full_name: "Admin One",
        memberships: [],
        is_platform_operator: true,
        operator_role: "admin",
      },
      orgId: null,
      ready: true,
    });
    renderAt("/admin");

    expect(screen.getByTestId("admin-entry")).toBeTruthy();
    expect(screen.queryByTestId("org-picker-page")).toBeNull();
  });

  it("keeps admin MFA-required /admin on admin entry rather than customer secure account", () => {
    setAuthFixture({
      me: {
        id: "admin-2",
        email: "admin2@example.test",
        full_name: "Admin Two",
        memberships: [{ org_id: "org-1", role: "admin" }],
        is_platform_operator: true,
        operator_role: "admin",
        second_factor_required: true,
      },
      orgId: "org-1",
      ready: true,
    });
    renderAt("/admin");

    expect(screen.getByTestId("admin-entry")).toBeTruthy();
    expect(screen.queryByTestId("secure-account-page")).toBeNull();
  });

  it("keeps /login on the customer login page", () => {
    setAuthFixture({ me: null, orgId: null, ready: true });
    renderAt("/login");

    expect(screen.getByTestId("login-page")).toBeTruthy();
    expect(screen.queryByTestId("admin-entry")).toBeNull();
  });

  it("keeps /signup on the customer signup page", () => {
    setAuthFixture({ me: null, orgId: null, ready: true });
    renderAt("/signup");

    expect(screen.getByTestId("signup-page")).toBeTruthy();
    expect(screen.queryByTestId("admin-entry")).toBeNull();
  });

  it("does not treat /administrator as an admin prefix and falls back to customer login", () => {
    setAuthFixture({ me: null, orgId: null, ready: true });
    renderAt("/administrator");

    expect(screen.getByTestId("login-page")).toBeTruthy();
    expect(screen.queryByTestId("admin-entry")).toBeNull();
  });

  it("routes orgless reviewer /ops to ops with step-up, not admin entry", () => {
    setAuthFixture({
      me: {
        id: "reviewer-1",
        email: "reviewer@example.test",
        full_name: "Reviewer One",
        memberships: [],
        is_platform_operator: true,
        operator_role: "reviewer",
      },
      orgId: null,
      ready: true,
    });
    renderAt("/ops");

    expect(screen.getByTestId("ops-page")).toBeTruthy();
    expect(screen.getByTestId("step-up-dialog")).toBeTruthy();
    expect(screen.queryByTestId("admin-entry")).toBeNull();
  });
});
