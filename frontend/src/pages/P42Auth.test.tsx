import { afterEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Me } from "@/auth/AuthContext";
import { authHeaders, type ApiClient } from "@/api/client";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { ForgotPasswordPage, ResetPasswordPage } from "@/pages/PasswordResetPages";
import { LoginPage } from "@/pages/LoginPage";
import { PasskeyGraceBanner } from "@/components/security/PasskeyGraceBanner";
import { RecoveryCodesCard } from "@/components/settings/AccountSecurityCards";

const ME: Me = {
  id: "u1",
  email: "owner@acme.test",
  full_name: "Owner",
  memberships: [{ org_id: "org-1", org_name: "Acme", org_slug: "acme", role_name: "owner" }],
  totp_enabled: false,
  has_passkey: true,
};

afterEach(() => {
  document.cookie = "csaas_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

describe("CSRF header", () => {
  it("is added to unsafe requests only", () => {
    document.cookie = "csaas_csrf=abc123; path=/";
    const api = { auth: { token: null, orgId: "org-1" } } as unknown as ApiClient;
    expect(authHeaders(api, "POST").get("X-CSRF-Token")).toBe("abc123");
    expect(authHeaders(api, "DELETE").get("X-CSRF-Token")).toBe("abc123");
    expect(authHeaders(api, "GET").get("X-CSRF-Token")).toBeNull();
    expect(authHeaders(api, "GET").get("Authorization")).toBeNull();
    expect(authHeaders(api, "GET").get("X-Org-Id")).toBe("org-1");
  });
});

describe("ForgotPasswordPage", () => {
  it("shows the same confirmation regardless of the account", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": new Error("401"),
      "/api/v1/auth/password/forgot": { status: "ok" },
    });
    renderWithProviders(<ForgotPasswordPage />, client);
    await userEvent.type(screen.getByLabelText("Email"), "someone@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText(/If an account exists for that email/)).toBeInTheDocument();
  });
});

describe("ResetPasswordPage", () => {
  it("refuses mismatched passwords without calling the API", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": new Error("401") });
    window.history.pushState({}, "", "/reset-password?token=abc");
    renderWithProviders(<ResetPasswordPage />, client);
    // MemoryRouter ignores window.location - no token -> incomplete link message.
    expect(await screen.findByText(/reset link is incomplete/)).toBeInTheDocument();
  });
});

describe("LoginPage recovery options", () => {
  it("switches to a recovery code at the second step", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": new Error("401"),
      "/api/v1/auth/login": { access_token: null, requires_2fa: true, pending_token: "p", methods: ["passkey"] },
      "/api/v1/auth/2fa/recovery": { access_token: null },
    });
    renderWithProviders(<LoginPage />, client);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await userEvent.click(await screen.findByRole("button", { name: "Use a recovery code" }));
    const input = screen.getByLabelText("Recovery code");
    await userEvent.type(input, "abcd-efgh-ijkm");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/auth/2fa/recovery")).toBe(true),
    );
    expect(screen.getByRole("button", { name: /Lost access/ })).toBeInTheDocument();
  });

  it("offers forgot password before signing in", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": new Error("401") });
    renderWithProviders(<LoginPage />, client);
    expect(screen.getByRole("link", { name: "Forgot your password?" })).toHaveAttribute("href", "/forgot-password");
  });
});

describe("PasskeyGraceBanner", () => {
  it("prompts privileged accounts without a passkey", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": { ...ME, has_passkey: false, passkey_required: true, passkey_grace_until: "2099-01-01T00:00:00Z" },
    });
    renderWithProviders(<PasskeyGraceBanner />, client);
    expect(await screen.findByText("Add a passkey to your account")).toBeInTheDocument();
  });

  it("stays hidden once a passkey exists", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": { ...ME, passkey_required: true } });
    renderWithProviders(<PasskeyGraceBanner />, client);
    await waitFor(() => expect(client.calls.length).toBeGreaterThan(0));
    expect(screen.queryByRole("status", { name: "Passkey required" })).toBeNull();
  });
});

describe("RecoveryCodesCard", () => {
  it("shows new codes once", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/recovery-codes": (_path: string, init: RequestInit) =>
        init.method === "POST" ? { codes: ["aaaa-bbbb-cccc", "dddd-eeee-ffff"] } : { remaining: 0 },
    });
    renderWithProviders(<RecoveryCodesCard />, client);
    await userEvent.click(await screen.findByRole("button", { name: "Generate recovery codes" }));
    expect(await screen.findByText("aaaa-bbbb-cccc")).toBeInTheDocument();
    expect(screen.getByText(/will not be shown again/)).toBeInTheDocument();
  });
});
