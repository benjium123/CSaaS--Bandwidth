import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import { ConversationHeader } from "./ConversationHeader";
import { SlaChip } from "./SlaChip";
import { InboxColumn } from "./InboxColumn";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import type { Conversation, Inbox } from "@/api/conversations";
import { slaState } from "@/api/inboxPro";

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [],
};

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    our_e164: "+14694617576",
    contact_e164: "+12145550111",
    inbox_id: "i1",
    thread_id: "t1",
    contact: { id: "c1", display_name: "Ella" },
    snippet: "Hello",
    last_event_type: "message",
    direction: "inbound",
    last_event_at: new Date().toISOString(),
    unread: false,
    status: "open",
    important: false,
    snoozed_until: null,
    sla: null,
    ...overrides,
  };
}

function renderHeader(
  conversation: Conversation,
  options: { canSend?: boolean; snoozeRoute?: RouteStub } = {},
) {
  const { canSend = true, snoozeRoute } = options;
  const client = makeStubClient({
    "/api/v1/conversations/t1/snooze":
      snoozeRoute ??
      ((_path: string, init: RequestInit & { json?: unknown }) =>
        init.method === "POST"
          ? { id: "t1", snoozed_until: (init.json as { until?: string }).until ?? null }
          : { id: "t1", snoozed_until: null }),
    "/api/v1/auth/me": ME,
    "/api/v1/conversations": (_path: string, _init: RequestInit & { json?: unknown }) => ({
      pages: [],
      pageParams: [],
    }),
  });

  const view = renderWithProviders(
    <SoftphoneProvider>
      <ConversationHeader conversation={conversation} canSend={canSend} />
    </SoftphoneProvider>,
    client,
  );
  return { ...view, client };
}

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

function renderColumn(
  overrides: Partial<React.ComponentProps<typeof InboxColumn>> = {},
) {
  const onSelect = overrides.onSelect ?? vi.fn();
  const props: React.ComponentProps<typeof InboxColumn> = {
    inboxes: [inbox()],
    isLoading: false,
    error: null,
    selection: { kind: "all" },
    onSelect,
    unread: {},
    unreadTruncated: false,
    ...overrides,
  };
  const client = makeStubClient({ "/api/v1/auth/me": ME });
  const view = renderWithProviders(<InboxColumn {...props} onSelect={onSelect} />, client);
  return { ...view, onSelect };
}

describe("SlaChip", () => {
  it("renders nothing when sla is null", () => {
    const { container } = render(<SlaChip sla={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders Overdue in a text-destructive element when breached", () => {
    render(<SlaChip sla={{ due_at: null, breached: true }} />);
    const chip = screen.getByLabelText("Reply time: Overdue");
    expect(chip).toHaveTextContent("Overdue");
    expect(chip).toHaveClass("text-destructive");
  });

  it("slaState says 45m left for due_at 45 minutes ahead", () => {
    const now = new Date("2025-01-01T12:00:00Z");
    const dueAt = new Date(now.getTime() + 45 * 60 * 1000).toISOString();
    expect(slaState({ due_at: dueAt, breached: false }, now)).toEqual({
      kind: "due",
      label: "45m left",
      minutes: 45,
    });
  });

  it("renders 45m left for due_at 45 minutes ahead", () => {
    const dueAt = new Date(Date.now() + 45 * 60 * 1000).toISOString();
    render(<SlaChip sla={{ due_at: dueAt, breached: false }} />);
    expect(screen.getByLabelText("Reply time: 45m left")).toHaveTextContent("45m left");
  });

  it("renders 2h left for 120 minutes ahead", () => {
    const dueAt = new Date(Date.now() + 120 * 60 * 1000).toISOString();
    render(<SlaChip sla={{ due_at: dueAt, breached: false }} />);
    expect(screen.getByLabelText("Reply time: 2h left")).toHaveTextContent("2h left");
  });
});

describe("ConversationHeader P26 snooze", () => {
  it("shows the chip for a conversation carrying sla", () => {
    renderHeader(
      conversation({
        sla: {
          due_at: new Date(Date.now() + 45 * 60 * 1000).toISOString(),
          breached: false,
        },
      }),
    );
    expect(screen.getByLabelText("Reply time: 45m left")).toBeInTheDocument();
  });

  it("disables the snooze trigger when thread_id is null, with its sentence", () => {
    renderHeader(conversation({ thread_id: null }));

    const trigger = screen.getByRole("button", { name: "Snooze" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute(
      "title",
      "This conversation has no messages yet - there is nothing to snooze",
    );
  });

  it("disables the snooze trigger for a read-only inbox", () => {
    renderHeader(conversation(), { canSend: false });

    const trigger = screen.getByRole("button", { name: "Snooze" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute(
      "title",
      "Read-only inbox - you can view but not snooze",
    );
  });

  it("opens the snooze menu and lists exactly the preset order", async () => {
    renderHeader(conversation());

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));

    const menu = screen.getByRole("menu", { name: "Snooze until" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent?.trim())).toEqual([
      "In 1 hour",
      "In 3 hours",
      "Tomorrow at 9am",
      "Next week",
      "Pick a date and time",
    ]);
  });

  it("picking In 1 hour POSTs to the snooze path with an until about one hour out", async () => {
    const { client } = renderHeader(conversation());

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "In 1 hour" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/conversations/t1/snooze" && c.init.method === "POST",
      );
      expect(call).toBeDefined();
      const until = new Date((call!.init.json as { until: string }).until).getTime();
      expect(Math.abs(until - (Date.now() + 60 * 60 * 1000))).toBeLessThan(5_000);
    });
  });

  it("closes the menu after a successful pick", async () => {
    renderHeader(conversation());

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "In 1 hour" }));

    await waitFor(() =>
      expect(screen.queryByRole("menu", { name: "Snooze until" })).not.toBeInTheDocument(),
    );
  });

  it("renders a role=alert when a snooze POST fails", async () => {
    renderHeader(conversation(), {
      snoozeRoute: (_path: string, _init: RequestInit & { json?: unknown }) => {
        throw new Error("Snooze failed");
      },
    });

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "In 1 hour" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Snooze failed");
  });

  it("shows a snoozed trigger, coming-back line, and Bring back now first item", async () => {
    renderHeader(
      conversation({
        snoozed_until: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      }),
    );

    const trigger = screen.getByRole("button", { name: "Snoozed - bring back now" });
    expect(trigger).toBeEnabled();
    expect(screen.getByText(/Snoozed - coming back/)).toBeInTheDocument();

    await userEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "Snooze until" });
    const first = within(menu).getAllByRole("menuitem")[0];
    expect(first).toHaveTextContent("Bring back now");
  });

  it("sends a DELETE to the same snooze path from Bring back now", async () => {
    const { client } = renderHeader(
      conversation({
        snoozed_until: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      }),
    );

    await userEvent.click(screen.getByRole("button", { name: "Snoozed - bring back now" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Bring back now" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/conversations/t1/snooze" && c.init.method === "DELETE",
      );
      expect(call).toBeDefined();
    });
  });

  it("reveals the custom datetime control and gates the Snooze button by validity", async () => {
    renderHeader(conversation());

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Pick a date and time" }));

    const input = screen.getByLabelText("Date and time");
    const button = screen.getByRole("button", { name: "Snooze until this time" });
    expect(input).toBeInTheDocument();
    expect(button).toBeDisabled();
    // Nothing typed yet, so there is nothing to complain about.
    expect(screen.queryByText("Choose a time in the future.")).toBeNull();

    await userEvent.type(input, "2020-01-01T00:00");
    expect(button).toBeDisabled();
    expect(screen.getByText("Choose a time in the future.")).toBeInTheDocument();

    await userEvent.clear(input);
    await userEvent.type(input, "2030-01-01T00:00");
    expect(button).toBeEnabled();
    expect(screen.queryByText("Choose a time in the future.")).toBeNull();
  });

  it("closes the snooze menu on Escape", async () => {
    renderHeader(conversation());

    await userEvent.click(screen.getByRole("button", { name: "Snooze" }));
    expect(screen.getByRole("menu", { name: "Snooze until" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("menu", { name: "Snooze until" })).not.toBeInTheDocument();
  });
});

describe("InboxColumn P26 snooze/overdue views", () => {
  it("renders Snoozed and Overdue rows and calls onSelect with view", async () => {
    const { onSelect } = renderColumn();

    await userEvent.click(screen.getByRole("button", { name: "Snoozed" }));
    expect(onSelect).toHaveBeenCalledWith({ kind: "view", view: "snoozed" });

    await userEvent.click(screen.getByRole("button", { name: "Overdue" }));
    expect(onSelect).toHaveBeenCalledWith({ kind: "view", view: "overdue" });
  });

  it("selected snoozed and overdue rows carry aria-current=true", () => {
    const first = renderColumn({ selection: { kind: "view", view: "snoozed" } });
    expect(screen.getByRole("button", { name: "Snoozed" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    first.unmount();

    renderColumn({ selection: { kind: "view", view: "overdue" } });
    expect(screen.getByRole("button", { name: "Overdue" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("renders the four view rows in order Important, Unresponded, Snoozed, Overdue", () => {
    renderColumn();

    const nav = screen.getByRole("navigation", { name: "Inboxes" });
    const labels = within(nav)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim())
      .filter(
        (label) =>
          label === "Important" ||
          label === "Unresponded" ||
          label === "Snoozed" ||
          label === "Overdue",
      );

    expect(labels).toEqual(["Important", "Unresponded", "Snoozed", "Overdue"]);
  });
});
