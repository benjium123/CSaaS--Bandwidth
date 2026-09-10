import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SessionOut } from "@/api/identity";
import { SessionsCard } from "./SessionsCard";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ME = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "acme", role_name: "owner" },
  ],
};

const SESSIONS: SessionOut[] = [
  {
    id: "s1",
    ip: "203.0.113.5",
    user_agent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    last_seen_at: "2024-01-01T00:00:00Z",
    created_at: "2024-01-01T00:00:00Z",
    expires_at: "2024-02-01T00:00:00Z",
    current: true,
  },
  {
    id: "s2",
    ip: "198.51.100.9",
    user_agent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 " +
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    last_seen_at: "2024-01-01T00:00:00Z",
    created_at: "2024-01-01T00:00:00Z",
    expires_at: "2024-02-01T00:00:00Z",
    current: false,
  },
];

describe("SessionsCard", () => {
  it("marks the session you are using and offers no way to revoke it", async () => {
    const client = makeStubClient({
      "/api/v1/me/sessions/revoke-all": { revoked: 3 },
      "/api/v1/me/sessions": SESSIONS,
      "/api/v1/auth/me": ME,
    });

    renderWithProviders(<SessionsCard />, client);

    await screen.findByText("Chrome on Windows");
    expect(screen.getByText("This device")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Sign out Chrome/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Sign out Safari on iPhone" })).toBeTruthy();
  });

  it("revokes one other session", async () => {
    const client = makeStubClient({
      "/api/v1/me/sessions/revoke-all": { revoked: 3 },
      "/api/v1/me/sessions": SESSIONS,
      "/api/v1/auth/me": ME,
    });

    renderWithProviders(<SessionsCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Sign out Safari on iPhone" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/me/sessions/s2" && call.init.method === "DELETE",
        ),
      ).toBe(true);
    });
  });

  it("sign out everywhere confirms first, keeps this device, and reports the count", async () => {
    const client = makeStubClient({
      "/api/v1/me/sessions/revoke-all": { revoked: 3 },
      "/api/v1/me/sessions": SESSIONS,
      "/api/v1/auth/me": ME,
    });

    renderWithProviders(<SessionsCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    expect(screen.getByText("This signs out every other device. You stay signed in here.")).toBeTruthy();
    expect(client.calls.some((call) => call.path.includes("revoke-all"))).toBe(false);

    await userEvent.click(await screen.findByRole("button", { name: "Sign out other devices" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/me/sessions/revoke-all" && call.init.method === "POST",
        ),
      ).toBe(true);
    });

    await screen.findByText("Signed out 3 other sessions.");
  });
});
