import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { OpsPage } from "./OpsPage";
import { TeamPage } from "./TeamPage";

const OPERATOR = {
  id: "op1",
  email: "op@platform.example",
  full_name: "Op",
  is_platform_operator: true,
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

const LOCKED_USER = {
  id: "u9",
  email: "pat@acme.com",
  full_name: "Pat",
  is_active: true,
  totp_enabled: true,
  has_passkey: false,
  recovery_codes_remaining: 3,
  locked_until: "2099-01-01T00:00:00Z",
  step_up_blocked_until: null,
};

describe("Operator console: Users", () => {
  it("finds an account and unlocks it", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": OPERATOR,
      "/api/v1/ops/queue": { applications: [], open_security_alerts: 0 },
      "/api/v1/ops/users": LOCKED_USER,
      "/api/v1/ops/users/u9/unlock": null,
      "/api/v1/ops/users/u9/reset-2fa": null,
    });
    renderWithProviders(<OpsPage />, client);

    await userEvent.click(await screen.findByRole("tab", { name: "Users" }));
    await userEvent.type(screen.getByLabelText("User email"), "pat@acme.com");
    await userEvent.click(screen.getByRole("button", { name: "Find" }));
    expect(await screen.findByText(/Locked until/)).toBeTruthy();

    // Resetting factors needs a written reason.
    expect(screen.getByRole("button", { name: "Reset 2FA" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/ops/users/u9/unlock")).toBe(true),
    );

    await userEvent.type(screen.getByLabelText("Support reason"), "Verified on video call");
    await userEvent.click(screen.getByRole("button", { name: "Reset 2FA" }));
    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/ops/users/u9/reset-2fa");
      expect(call?.init.json).toEqual({ reason: "Verified on video call" });
    });
  });
});

describe("Team page: reset a member's 2FA", () => {
  it("asks for confirmation, then resets", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": {
        ...OPERATOR,
        permissions: ["members:read", "members:update"],
        memberships: [
          {
            org_id: "org-1",
            org_name: "Org",
            org_slug: "org",
            role_name: "admin",
            permissions: ["members:read", "members:update"],
          },
        ],
      },
      "/api/v1/me/capabilities": {
        permissions: ["members:read", "members:update"],
        org: { has_provider: false, has_number: false, member_count: 2, registration_state: "none" },
      },
      "/api/v1/orgs/current/members": [
        { user_id: "op1", email: "op@platform.example", full_name: "Op", role_name: "admin" },
        { user_id: "u2", email: "agent@acme.com", full_name: "Agent", role_name: "agent" },
      ],
      "/api/v1/orgs/current/invites": [],
      "/api/v1/roles": [],
      "/api/v1/roles/permissions": [],
      "/api/v1/orgs/current/members/u2/reset-2fa": null,
    });
    renderWithProviders(<TeamPage />, client);

    const buttons = await screen.findAllByRole("button", { name: "Reset 2FA" });
    expect(buttons).toHaveLength(1); // never offered on your own row
    await userEvent.click(buttons[0]);
    await userEvent.click(screen.getByRole("button", { name: "Confirm reset" }));
    expect(await screen.findByText("2FA reset")).toBeTruthy();
  });
});
