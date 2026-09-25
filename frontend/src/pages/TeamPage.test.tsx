import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Me } from "@/auth/AuthContext";
import { TeamPage } from "./TeamPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const MEMBERS = [
  { user_id: "u1", email: "owner@example.com", full_name: "Owner Person", role_name: "owner" },
];

const INVITES = [
  {
    id: "inv-1",
    email: "pending@example.com",
    role_name: "agent",
    expires_at: "2999-01-01T00:00:00Z",
    accepted_at: null,
    revoked_at: null,
  },
];

const CREATED_INVITE = {
  id: "inv-2",
  email: "new@example.com",
  role_name: "agent",
  expires_at: "2999-01-01T00:00:00Z",
  accepted_at: null,
  revoked_at: null,
  token: "raw-token-abc",
  accept_url: "https://app.example.com/accept-invite?token=raw-token-abc",
};

const ROLES = [
  {
    id: "role-agent",
    name: "agent",
    permissions: ["contacts:read", "contacts:write", "inbox:read"],
    is_system: true,
    member_count: 3,
  },
  {
    id: "role-admin",
    name: "admin",
    permissions: ["contacts:read", "contacts:assign"],
    is_system: true,
    member_count: 1,
  },
  {
    id: "role-custom",
    name: "Team lead",
    permissions: ["contacts:read"],
    is_system: false,
    member_count: 0,
  },
];

const ROLES_WITH_IN_USE_ROLE = [
  ...ROLES.slice(0, 2),
  {
    id: "role-custom",
    name: "Team lead",
    permissions: ["contacts:read"],
    is_system: false,
    member_count: 2,
  },
];

const ME_WITH_ROLES_WRITE: Me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "owner",
      permissions: ["roles:write", "contacts:read", "contacts:write", "inbox:read"],
    },
  ],
};

const ME_NO_ROLES_WRITE: Me = {
  id: "u2",
  email: "agent@example.com",
  full_name: "Agent Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "agent",
      permissions: ["contacts:read"],
    },
  ],
};

describe("TeamPage", () => {
  it("renders members + invitations and shows the one-time link after inviting someone", async () => {
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": ((_path, init) => {
        if (init.method === "POST") return CREATED_INVITE;
        return INVITES;
      }) as RouteStub,
    });
    renderWithProviders(<TeamPage />, client);

    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(await screen.findByText("pending@example.com")).toBeInTheDocument();

    const roleSelect = screen.getByLabelText("Role") as HTMLSelectElement;
    const optionLabels = within(roleSelect)
      .getAllByRole("option")
      .map((option) => option.textContent?.toLowerCase());
    expect(optionLabels).toEqual(expect.arrayContaining(["admin", "agent"]));
    expect(optionLabels).not.toContain("owner");

    await userEvent.type(screen.getByLabelText("Email"), "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Send invite" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/orgs/current/invites" && call.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (call) => call.path === "/api/v1/orgs/current/invites" && call.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({ email: "new@example.com", role_name: "agent" });

    expect(await screen.findByDisplayValue(CREATED_INVITE.accept_url)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });

  it("shows a retry affordance when members fail to load, and recovers on retry", async () => {
    let membersCalls = 0;
    const client = makeStubClient({
      "/api/v1/orgs/current/members": (() => {
        membersCalls += 1;
        if (membersCalls === 1) return new Error("Failed to load members");
        return MEMBERS;
      }) as RouteStub,
      "/api/v1/orgs/current/invites": INVITES,
    });
    renderWithProviders(<TeamPage />, client);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load members");

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("fetches roles only after switching to the Roles tab and lists them", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_WITH_ROLES_WRITE,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/roles": ROLES,
    });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("owner@example.com");
    expect(client.calls.some((c) => c.path === "/api/v1/roles")).toBe(false);

    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));

    expect(await screen.findByText("Team lead")).toBeInTheDocument();
    expect(screen.getByText("3 members")).toBeInTheDocument();
    expect(screen.getAllByText("Built-in").length).toBe(2);
  });

  it("creates a role from the Agent starting point", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_WITH_ROLES_WRITE,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/roles": ROLES,
    });
    renderWithProviders(<TeamPage />, client);

    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("Team lead");

    await userEvent.click(screen.getByRole("button", { name: "New role" }));
    expect(screen.getByLabelText("Can listen in on live calls")).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Agent starting point" }));

    expect(screen.getByLabelText("Can see contacts")).toBeChecked();

    await userEvent.type(screen.getByLabelText("Role name"), "Team lead plus");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/roles" && entry.init.method === "POST",
      );
      expect(call?.init.json).toEqual({
        name: "Team lead plus",
        permissions: ["contacts:read", "contacts:write", "inbox:read"],
        clone_from: "role-agent",
      });
    });
  });

  it("opens a built-in role read-only and disables its Delete button", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_WITH_ROLES_WRITE,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/roles": ROLES,
    });
    renderWithProviders(<TeamPage />, client);

    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("Team lead");

    const row = screen.getByText("agent").closest("tr") as HTMLTableRowElement;
    await userEvent.click(within(row).getByRole("button", { name: "View" }));

    expect(await screen.findByText("Built-in roles can't be edited.")).toBeInTheDocument();
    expect(screen.getByLabelText("Can see contacts")).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Delete" })).toHaveAttribute(
      "title",
      "Built-in roles can't be deleted.",
    );
  });

  it("cannot delete a role that is still assigned to someone", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_WITH_ROLES_WRITE,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/roles": ROLES_WITH_IN_USE_ROLE,
    });
    renderWithProviders(<TeamPage />, client);

    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("Team lead");

    const row = screen.getByText("Team lead").closest("tr") as HTMLTableRowElement;
    const deleteButton = within(row).getByRole("button", { name: "Delete" });
    expect(deleteButton).toBeDisabled();
    expect(deleteButton).toHaveAttribute(
      "title",
      "This role is still assigned to someone.",
    );
  });

  it("disables New role if the user lacks roles:write", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_NO_ROLES_WRITE,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/roles": ROLES,
    });
    renderWithProviders(<TeamPage />, client);

    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("Team lead");

    const newRoleButton = screen.getByRole("button", { name: "New role" });
    expect(newRoleButton).toBeDisabled();
    expect(newRoleButton).toHaveAttribute(
      "title",
      "You don't have permission to change roles.",
    );
  });
});

describe("TeamPage seats", () => {
  const OWNER: Me = {
    ...ME_WITH_ROLES_WRITE,
    memberships: [
      { ...ME_WITH_ROLES_WRITE.memberships[0], permissions: ["members:invite", "members:read"] },
    ],
  };
  const stubs = (seats: unknown) =>
    makeStubClient({
      "/api/v1/auth/me": OWNER,
      "/api/v1/me/capabilities": {
        permissions: ["members:invite", "members:read"],
        org: { has_provider: true, has_number: true, member_count: 2, registration_state: "approved" },
      },
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/orgs/current/invites": INVITES,
      "/api/v1/orgs/current/seats": seats,
      "/api/v1/billing/plan": {
        plan: { code: "team", name: "Team", status: "active", price_cents: 4500, monthly_total_cents: 4500, renews_at: null, cancel_at_period_end: false },
        users: { limit: 2, in_use: 2, included: 3, extra: 0 },
        numbers: { limit: 3, in_use: 3, included: 3, extra: 0 },
        extra_user_cents: 1500, extra_number_cents: 500, minutes_per_user: 200, catalog: [],
      },
      "/api/v1/billing/plan/users": { plan: null, users: { limit: 3, in_use: 2 }, numbers: { limit: 3, in_use: 3 }, extra_user_cents: 1500, extra_number_cents: 500, minutes_per_user: 200, catalog: [] },
    });

  it("when every user on the plan is taken, the owner buys one at $15 after seeing the price", async () => {
    const client = stubs({ enforced: true, limit: 2, members: 1, pending_invites: 1, available: 0 });
    renderWithProviders(<TeamPage />, client);
    const usage = await screen.findByTestId("seat-usage");
    expect(usage.textContent?.trim()).toBe("2 of 2 user seats used on your plan. Add a user");

    await userEvent.click(screen.getByRole("button", { name: "Add teammate" }));
    const panel = await screen.findByRole("region", { name: "Add a user" });
    expect(panel).toHaveTextContent("All 2 users on your Team plan are in use");
    expect(panel).toHaveTextContent("A number for them is $5/month more.");
    expect(client.calls.some(c => c.path === "/api/v1/billing/plan/users")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Add a user for $15/month" }));
    await waitFor(() =>
      expect(client.calls.find(c => c.path === "/api/v1/billing/plan/users")?.init.json).toEqual({
        count: 1,
        accept_cents: 1500,
      }),
    );
  });

  it("offers no buy link while a seat is free", async () => {
    renderWithProviders(
      <TeamPage />,
      stubs({ enforced: true, limit: 3, members: 1, pending_invites: 1, available: 1 }),
    );
    expect((await screen.findByTestId("seat-usage")).textContent).toContain("2 of 3 user seats");
    expect(screen.queryByRole("button", { name: "Add a user" })).toBeNull();
  });

  it("shows nothing about seats for a workspace that is not seat-limited", async () => {
    renderWithProviders(
      <TeamPage />,
      stubs({ enforced: false, limit: null, members: 4, pending_invites: 0, available: null }),
    );
    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(screen.queryByTestId("seat-usage")).toBeNull();
  });
});
