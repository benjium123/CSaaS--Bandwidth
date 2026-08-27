import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
      .map((o) => o.textContent?.toLowerCase());
    expect(optionLabels).toEqual(expect.arrayContaining(["admin", "agent"]));
    expect(optionLabels).not.toContain("owner");

    await userEvent.type(screen.getByLabelText("Email"), "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Send invite" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/orgs/current/invites" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/orgs/current/invites" && c.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({ email: "new@example.com", role_name: "agent" });

    expect(await screen.findByDisplayValue(CREATED_INVITE.accept_url)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });
});
