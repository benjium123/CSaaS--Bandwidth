import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TextingStatusBanner } from "./TextingStatusBanner";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const AUTH_ME = {
  id: "u1",
  email: "o@example.com",
  full_name: "O",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

function note(id: string, kind: string, body: string, created_at: string, read_at: string | null = null) {
  return {
    id, kind, body, created_at, read_at,
    thread_id: null, our_e164: null, contact_e164: null,
  };
}

function client(items: unknown[]) {
  return makeStubClient({
    "/api/v1/auth/me": AUTH_ME,
    "/api/v1/me/notifications?limit=50": { items, unread_count: items.length },
    "/api/v1/me/notifications/read": { updated: 1 },
  });
}

describe("TextingStatusBanner", () => {
  it("shows the newest unread texting notice with a Review link", async () => {
    renderWithProviders(
      <TextingStatusBanner />,
      client([
        note("a", "texting", "Your business was approved for texting", "2026-10-18T10:00:00Z"),
        note("b", "texting", "Your texting campaign needs changes: Opt-in is not described", "2026-10-18T11:00:00Z"),
        note("c", "low_balance", "Your balance is running low", "2026-10-18T12:00:00Z"),
        note("d", "texting", "Your texting registration was submitted", "2026-10-18T13:00:00Z", "2026-10-18T13:05:00Z"),
      ]),
    );
    expect(
      await screen.findByText("Your texting campaign needs changes: Opt-in is not described"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/settings/messaging");
    expect(screen.getByRole("status")).toHaveAttribute("data-tone", "warning");
  });

  it("is hidden with no unread texting notice", async () => {
    const c = client([note("c", "low_balance", "Low", "2026-10-18T12:00:00Z")]);
    renderWithProviders(<TextingStatusBanner />, c);
    await waitFor(() => expect(c.calls.some((x) => x.path.includes("/me/notifications"))).toBe(true));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("dismiss marks the notice read", async () => {
    const c = client([note("a", "texting", "Your business was approved for texting", "2026-10-18T10:00:00Z")]);
    renderWithProviders(<TextingStatusBanner />, c);
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
    await waitFor(() => {
      const call = c.calls.find((x) => x.path === "/api/v1/me/notifications/read");
      expect(call?.init.json).toEqual({ ids: ["a"] });
    });
  });
});
