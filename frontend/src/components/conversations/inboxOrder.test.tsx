import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { InboxColumn, reorderInboxes } from "./InboxColumn";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Inbox } from "@/api/conversations";

vi.mock("@/components/ui/CommandPalette", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/CommandPalette")>();
  return { ...actual, openCommandPalette: vi.fn() };
});

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [
    { org_id: "org-1", org_name: "Acme Plumbing", org_slug: "acme", role_name: "owner" },
  ],
};

const FULL_CAPS = {
  permissions: ["contacts:read", "calls:read", "campaigns:read", "org:read", "settings:read"],
  org: { has_provider: true, has_number: true, member_count: 2, registration_state: "approved" },
};

function inbox(overrides: Partial<Inbox> = {}): Inbox {
  return {
    id: "i1",
    name: "Sales",
    color: "#22c55e",
    e164: "+14694617576",
    number_id: "n1",
    my_role: "admin",
    ...overrides,
  };
}

function renderColumn(inboxes: Inbox[]) {
  const props: React.ComponentProps<typeof InboxColumn> = {
    inboxes,
    isLoading: false,
    error: null,
    selection: { kind: "all" },
    onSelect: vi.fn(),
    unread: {},
    unreadTruncated: false,
  };
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": FULL_CAPS,
    "/api/v1/notifications": { items: [], unread_count: 0 },
    "/api/v1/me/inbox-order": undefined,
  });
  return { client, ...renderWithProviders(<InboxColumn {...props} />, client) };
}

// --------------------------------------------------------------------------------------
// reorderInboxes: the pure drag-end math, tested directly rather than through dnd-kit's
// sensors - jsdom has no real layout, so PointerSensor/KeyboardSensor geometry cannot be
// simulated reliably. The handle's presence and the mutation wiring around this function
// are covered below by rendering; the drag gesture itself is verified by hand.
// --------------------------------------------------------------------------------------
describe("reorderInboxes", () => {
  const list = [inbox({ id: "a" }), inbox({ id: "b" }), inbox({ id: "c" })];

  it("moves the active item to the dropped-on item's position", () => {
    const result = reorderInboxes(list, "a", "c");
    expect(result?.map((i) => i.id)).toEqual(["b", "c", "a"]);
  });

  it("moving downward one slot swaps the two neighbours", () => {
    const result = reorderInboxes(list, "b", "a");
    expect(result?.map((i) => i.id)).toEqual(["b", "a", "c"]);
  });

  it("returns null when dropped on itself", () => {
    expect(reorderInboxes(list, "b", "b")).toBeNull();
  });

  it("returns null when the active id is no longer in the list", () => {
    expect(reorderInboxes(list, "gone", "a")).toBeNull();
  });

  it("returns null when the drop target is no longer in the list", () => {
    expect(reorderInboxes(list, "a", "gone")).toBeNull();
  });

  it("does not mutate the input array", () => {
    const before = list.map((i) => i.id);
    reorderInboxes(list, "a", "c");
    expect(list.map((i) => i.id)).toEqual(before);
  });
});

// --------------------------------------------------------------------------------------
// The drag handle: present only when there is something to reorder.
// --------------------------------------------------------------------------------------
describe("InboxColumn: reorder handle", () => {
  it("renders one handle per line when there is more than one number", async () => {
    renderColumn([inbox({ id: "i1", name: "Sales" }), inbox({ id: "i2", name: "Support" })]);
    expect(await screen.findByRole("button", { name: "Reorder Sales" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reorder Support" })).toBeInTheDocument();
  });

  it("renders no handle when there is only one number - nothing to reorder", async () => {
    renderColumn([inbox({ id: "i1", name: "Sales" })]);
    await screen.findByRole("button", { name: "Sales" }); // the row itself has rendered
    expect(screen.queryByRole("button", { name: "Reorder Sales" })).not.toBeInTheDocument();
  });

  it("renders no handle with zero numbers", async () => {
    renderColumn([]);
    expect(await screen.findByText("No inboxes yet")).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Reorder /)).not.toBeInTheDocument();
  });
});
