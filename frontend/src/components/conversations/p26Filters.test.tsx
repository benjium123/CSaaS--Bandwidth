import { useState } from "react";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Conversation, ConversationFilter, ConversationTab } from "@/api/conversations";
import {
  ConversationList,
  FilterChips,
  FILTER_CHIPS,
  MAX_VISIBLE_CHIPS,
  type FilterChip,
} from "./ConversationList";

function FilterChipsToggleHarness({
  initialFilter,
  onFilterChange,
}: {
  initialFilter: ConversationFilter;
  onFilterChange: (filter: ConversationFilter) => void;
}) {
  const [filter, setFilter] = useState<ConversationFilter>(initialFilter);
  return (
    <FilterChips
      filter={filter}
      onFilterChange={(next) => {
        setFilter(next);
        onFilterChange(next);
      }}
    />
  );
}

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    our_e164: "+15005550006",
    contact_e164: "+15005550001",
    inbox_id: "inbox-1",
    thread_id: "thread-1",
    contact: { id: "contact-1", display_name: "Ada Lovelace" },
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

function renderList(items: Conversation[]) {
  const client = makeStubClient({});
  const props: {
    items: Conversation[];
    selectedContactE164: string | null;
    onSelect: (contactE164: string) => void;
    tab: ConversationTab;
    onTabChange: (tab: ConversationTab) => void;
    filter: ConversationFilter;
    onFilterChange: (filter: ConversationFilter) => void;
    q: string;
    onQChange: (q: string) => void;
    hasNextPage: boolean;
    isFetchingNextPage: boolean;
    isLoading: boolean;
    onLoadMore: () => void;
    error?: string | null;
    hasNoInboxAccess?: boolean;
  } = {
    items,
    selectedContactE164: null,
    onSelect: () => {},
    tab: "chats",
    onTabChange: () => {},
    filter: "open",
    onFilterChange: () => {},
    q: "",
    onQChange: () => {},
    hasNextPage: false,
    isFetchingNextPage: false,
    isLoading: false,
    onLoadMore: () => {},
    error: null,
    hasNoInboxAccess: false,
  };

  return renderWithProviders(<ConversationList {...props} />, client);
}

const sixChips: FilterChip[] = [...FILTER_CHIPS, { filter: "all", label: "All" }];
// Fable decision: MAX_VISIBLE_CHIPS dropped to 4, so the real FILTER_CHIPS set (5) now
// collapses by default. This four-item slice exercises the direct-button branch that
// FILTER_CHIPS itself no longer reaches.
const fourChips: FilterChip[] = FILTER_CHIPS.slice(0, 4);

describe("p26 filter chips", () => {
  it("collapse rule is live: the five default chips already exceed the limit", () => {
    // Previously this pinned FILTER_CHIPS.length === MAX_VISIBLE_CHIPS (dormant rule).
    // Fable chose to ship the collapsed form now: dropping the limit below the default
    // chip count so the dropdown renders without waiting for a sixth chip.
    expect(MAX_VISIBLE_CHIPS).toBe(4);
    expect(FILTER_CHIPS.length).toBeGreaterThan(MAX_VISIBLE_CHIPS);
  });

  it("collapses the default five chips into a single Filter trigger", () => {
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="open" onFilterChange={onFilterChange} />, client);

    expect(screen.getByRole("button", { name: /^Filter/ })).toBeTruthy();
    for (const chip of FILTER_CHIPS) {
      expect(screen.queryByRole("button", { name: chip.label })).toBeNull();
    }
  });

  it("renders four chips as buttons without a Filter trigger, at the collapse limit", () => {
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={fourChips} filter="open" onFilterChange={onFilterChange} />,
      client,
    );

    for (const chip of fourChips) {
      expect(screen.getByRole("button", { name: chip.label })).toBeTruthy();
    }
    expect(screen.queryByRole("button", { name: /^Filter/ })).toBeNull();
  });

  it("marks only the active chip with aria-pressed=true, at the collapse limit", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={fourChips} filter="snoozed" onFilterChange={() => {}} />,
      client,
    );

    for (const chip of fourChips) {
      expect(
        screen.getByRole("button", { name: chip.label }).getAttribute("aria-pressed"),
      ).toBe(chip.filter === "snoozed" ? "true" : "false");
    }
  });

  it("toggles Snoozed on and back to open via the collapsed default menu", async () => {
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChipsToggleHarness initialFilter="open" onFilterChange={onFilterChange} />,
      client,
    );

    await user.click(screen.getByRole("button", { name: /^Filter/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "Snoozed" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("snoozed");

    await user.click(screen.getByRole("button", { name: /^Filter/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "Snoozed" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("open");
  });

  it("clicking Overdue in the collapsed default menu calls onFilterChange with overdue", async () => {
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="open" onFilterChange={onFilterChange} />, client);

    await user.click(screen.getByRole("button", { name: /^Filter/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "Overdue" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("overdue");
  });

  it("marks only the active chip as checked in the collapsed default menu", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="snoozed" onFilterChange={() => {}} />, client);

    await user.click(screen.getByRole("button", { name: /^Filter/ }));
    for (const chip of FILTER_CHIPS) {
      expect(
        screen.getByRole("menuitemradio", { name: chip.label }).getAttribute("aria-checked"),
      ).toBe(chip.filter === "snoozed" ? "true" : "false");
    }
  });

  it("replaces six chips with one Filter trigger and hides the chip buttons", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={sixChips} filter="open" onFilterChange={() => {}} />,
      client,
    );

    expect(screen.getByRole("button", { name: "Filter" })).toBeTruthy();
    for (const chip of sixChips) {
      expect(screen.queryByRole("button", { name: chip.label })).toBeNull();
    }
  });

  it("lists all six chips as menuitemradio and picks one", async () => {
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={sixChips} filter="open" onFilterChange={onFilterChange} />,
      client,
    );

    await user.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(sixChips.length);

    await user.click(screen.getByRole("menuitemradio", { name: "Overdue" }));
    expect(onFilterChange).toHaveBeenCalledWith("overdue");
  });

  it("shows the active filter in the collapsed trigger text", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={sixChips} filter="unread" onFilterChange={() => {}} />,
      client,
    );

    expect(screen.getByRole("button", { name: "Filter: Unread" })).toBeTruthy();
  });

  it("closes the collapsed menu on Escape and on an outside mousedown", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChips chips={sixChips} filter="open" onFilterChange={() => {}} />,
      client,
    );

    await user.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(sixChips.length);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryAllByRole("menuitemradio")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(sixChips.length);

    fireEvent.mouseDown(document.body);
    expect(screen.queryAllByRole("menuitemradio")).toHaveLength(0);
  });

  it("names the Open/All menu after the active filter, including the two new ones", () => {
    // The old ternary chain fell through to "Unresponded" for anything it did not name,
    // so adding Snoozed and Overdue would have made the menu lie about what is on.
    const client = makeStubClient({});

    for (const [filter, label] of [
      ["snoozed", "Snoozed"],
      ["overdue", "Overdue"],
      ["unresponded", "Unresponded"],
    ] as const) {
      const view = renderWithProviders(
        <ConversationList
          items={[]}
          selectedContactE164={null}
          onSelect={() => {}}
          tab="chats"
          onTabChange={() => {}}
          filter={filter}
          onFilterChange={() => {}}
          q=""
          onQChange={() => {}}
          hasNextPage={false}
          isFetchingNextPage={false}
          isLoading={false}
          onLoadMore={() => {}}
        />,
        client,
      );
      // The chip with the same word is also on screen, so pick the menu TRIGGER by its
      // aria-haspopup rather than by name.
      const trigger = view.container.querySelector('button[aria-haspopup="menu"]');
      expect(trigger?.textContent).toContain(label);
      view.unmount();
    }
  });

  it("shows Overdue in a row whose sla is breached", () => {
    renderList([conversation({ sla: { due_at: null, breached: true } })]);
    const row = screen.getAllByRole("listitem")[0];
    expect(within(row).getAllByText("Overdue").length).toBeGreaterThan(0);
  });

  it("shows a left countdown in a row whose sla is due", () => {
    const dueAt = new Date(Date.now() + 30 * 60 * 1000).toISOString();
    renderList([conversation({ sla: { due_at: dueAt, breached: false } })]);
    const row = screen.getAllByRole("listitem")[0];
    expect(within(row).getByText(/left/)).toBeTruthy();
  });

  it("shows neither Overdue nor a countdown when a row has no sla", () => {
    renderList([conversation({ sla: null })]);
    const row = screen.getAllByRole("listitem")[0];
    expect(within(row).queryByText("Overdue")).toBeNull();
    expect(within(row).queryByText(/left/)).toBeNull();
  });
});
