import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxColumn } from "./InboxColumn";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Inbox } from "@/api/conversations";

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [],
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

const inboxes = [
  inbox(),
  inbox({
    id: "i2",
    name: "Support",
    color: "#3b82f6",
    e164: "+12145550111",
    number_id: "n2",
  }),
];

function renderColumn(
  overrides: Partial<React.ComponentProps<typeof InboxColumn>> = {},
) {
  const props: React.ComponentProps<typeof InboxColumn> = {
    inboxes,
    isLoading: false,
    error: null,
    selection: { kind: "all" },
    onSelect: vi.fn(),
    unread: {},
    unreadTruncated: false,
    ...overrides,
  };
  const client = makeStubClient({ "/api/v1/auth/me": ME });
  return renderWithProviders(<InboxColumn {...props} />, client);
}

describe("InboxColumn", () => {
  it('renders "All conversations"', () => {
    renderColumn();

    expect(
      screen.getByRole("button", { name: "All conversations" }),
    ).toBeInTheDocument();
  });

  it("renders one row per inbox with its name", () => {
    renderColumn();

    const nav = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(nav).getByText("Sales")).toBeInTheDocument();
    expect(within(nav).getByText("Support")).toBeInTheDocument();
  });

  it("the colour dot carries the inbox colour", () => {
    renderColumn();

    const row = screen.getByRole("button", { name: "Sales" });
    const dot = row.querySelector('span[aria-hidden="true"]');
    expect(dot).toHaveStyle({ backgroundColor: "#22c55e" });
  });

  it("an unread count badge appears only when > 0", () => {
    renderColumn({ unread: {} });
    expect(screen.queryByLabelText("2 unread")).not.toBeInTheDocument();

    renderColumn({ unread: { i2: 2 } });
    expect(screen.getByLabelText("2 unread")).toHaveTextContent("2");
  });

  it("a truncated count renders as N+ with a truthful title", () => {
    renderColumn({ unread: { i1: 8 }, unreadTruncated: true });

    const badge = screen.getByLabelText("8 unread");
    expect(badge).toHaveTextContent("8+");
    expect(badge).toHaveAttribute(
      "title",
      "Only the most recent unread conversations are counted",
    );
  });

  it('"Important" and "Unresponded" rows call onSelect with {kind:"view"}', async () => {
    const onSelect = vi.fn();
    renderColumn({ onSelect });

    await userEvent.click(screen.getByRole("button", { name: "Important" }));
    expect(onSelect).toHaveBeenCalledWith({ kind: "view", view: "important" });

    await userEvent.click(screen.getByRole("button", { name: "Unresponded" }));
    expect(onSelect).toHaveBeenCalledWith({ kind: "view", view: "unresponded" });
  });

  it('the selected row carries aria-current="true"', () => {
    renderColumn({ selection: { kind: "inbox", inboxId: "i1" } });

    expect(screen.getByRole("button", { name: "Sales" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it('"+ New" is disabled with the read-only title when canCompose is false', () => {
    renderColumn({
      onNew: vi.fn(),
      canCompose: false,
      canComposeLoading: false,
    });

    const newButton = screen.getByRole("button", { name: "New conversation" });
    expect(newButton).toBeDisabled();
    expect(newButton).toHaveAttribute(
      "title",
      "Read-only inbox — you can view but not start new conversations",
    );
  });

  it("the no-inbox empty state does not repeat the conversation list's sentence", () => {
    renderColumn({ inboxes: [], isLoading: false, error: null });

    expect(screen.getByText("No inboxes yet")).toBeInTheDocument();
    // The teaching sentence lives in ONE place - the conversation list - so the two
    // columns never show the same error twice.
    expect(
      screen.queryByText("You have no inbox access yet — ask an admin"),
    ).not.toBeInTheDocument();
  });
});
