import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { LoginEventOut } from "@/api/identity";
import { LoginHistoryCard } from "./LoginHistoryCard";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const caps = (permissions: string[]) => ({
  permissions,
  org: {
    has_provider: false,
    has_number: false,
    member_count: 1,
    registration_state: "none",
  },
});

const ME = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "acme", role_name: "owner" },
  ],
};

const ORG_EVENTS: LoginEventOut[] = [
  {
    id: "event-1",
    at: "2024-01-01T00:00:00Z",
    ip: "203.0.113.5",
    user_agent: null,
    outcome: "ok",
    detail: null,
    email: "a@example.com",
    user_id: "u1",
  },
  {
    id: "event-2",
    at: "2024-01-02T00:00:00Z",
    ip: "198.51.100.9",
    user_agent: null,
    outcome: "bad_password",
    detail: null,
    email: "b@example.com",
    user_id: "u2",
  },
  {
    id: "event-3",
    at: "2024-01-03T00:00:00Z",
    ip: "192.0.2.10",
    user_agent: null,
    outcome: "blocked_ip",
    detail: null,
    email: "c@example.com",
    user_id: "u3",
  },
];

describe("LoginHistoryCard", () => {
  it("hides the workspace history from members without members:read", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/me/capabilities": caps([]),
    });

    renderWithProviders(<LoginHistoryCard scope="org" />, client);

    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/me/capabilities")).toBe(true);
    });

    expect(screen.queryByText("Workspace sign-in history")).toBeNull();
    expect(
      client.calls.some((call) => call.path.includes("/api/v1/orgs/current/login-events")),
    ).toBe(false);
  });

  it("shows the workspace history with members:read and labels outcomes in plain language", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/me/capabilities": caps(["members:read"]),
      "/api/v1/orgs/current/login-events": ORG_EVENTS,
    });

    renderWithProviders(<LoginHistoryCard scope="org" />, client);

    await screen.findByText("Signed in");
    expect(screen.getByText("Wrong password")).toBeTruthy();
    expect(screen.getByText("Blocked by IP allowlist")).toBeTruthy();
  });

  it("filters by outcome without refetching", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/me/capabilities": caps(["members:read"]),
      "/api/v1/orgs/current/login-events": ORG_EVENTS,
    });

    renderWithProviders(<LoginHistoryCard scope="org" />, client);

    await screen.findByText("Signed in");

    const before = client.calls.filter((call) =>
      call.path.includes("/api/v1/orgs/current/login-events"),
    ).length;

    await userEvent.selectOptions(screen.getByLabelText("Filter by outcome"), "bad_password");

    await waitFor(() => {
      expect(screen.queryByText("a@example.com")).toBeNull();
      expect(screen.getByText("b@example.com")).toBeTruthy();
      expect(screen.queryByText("c@example.com")).toBeNull();
    });

    const after = client.calls.filter((call) =>
      call.path.includes("/api/v1/orgs/current/login-events"),
    ).length;

    expect(after).toBe(before);
  });

  it("the personal history does not show a Who column", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/me/login-events": [
        {
          id: "personal-1",
          at: "2024-01-01T00:00:00Z",
          ip: "203.0.113.5",
          user_agent: null,
          outcome: "ok",
          detail: null,
          email: "a@example.com",
          user_id: "u1",
        },
      ],
    });

    renderWithProviders(<LoginHistoryCard scope="me" />, client);

    await screen.findByText("Your recent sign-ins");
    expect(screen.queryByText("Who")).toBeNull();
  });
});
