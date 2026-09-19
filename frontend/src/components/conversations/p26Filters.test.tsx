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
  FILTER_LABELS,
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

// The collapse branch is no longer reachable through FILTER_CHIPS itself (three chips,
// limit four), so these synthetic sets keep both branches of FilterChips under test.
const sixChips: FilterChip[] = [
  ...FILTER_CHIPS,
  { filter: "unread", label: "Unread" },
  { filter: "snoozed", label: "Snoozed" },
  { filter: "overdue", label: "Overdue" },
];
const fourChips: FilterChip[] = [...FILTER_CHIPS, { filter: "unread", label: "Unread" }];

describe("p26 filter chips", () => {
  // console-reference.html: "four reduced to three - All, Unresponded, Important."
  it("is exactly three chips, in the reference's order", () => {
    expect(FILTER_CHIPS.map((chip) => chip.label)).toEqual([
      "All",
      "Unresponded",
      "Important",
    ]);
    expect(FILTER_CHIPS.map((chip) => chip.filter)).toEqual([
      "all",
      "unresponded",
      "important",
    ]);
    // Three is under the collapse limit, so they render as pills, not a dropdown.
    expect(MAX_VISIBLE_CHIPS).toBe(4);
    expect(FILTER_CHIPS.length).toBeLessThanOrEqual(MAX_VISIBLE_CHIPS);
  });

  it("renders the three default chips as buttons, with no Filter trigger", () => {
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="open" onFilterChange={onFilterChange} />, client);

    for (const chip of FILTER_CHIPS) {
      expect(screen.getByRole("button", { name: chip.label })).toBeTruthy();
    }
    expect(screen.queryByRole("button", { name: /^Filter/ })).toBeNull();
  });

  // Unread / Snoozed / Overdue were dropped AS FILTERS, not as behaviour: the unread dot
  // and the SLA "Overdue" chip still render on a row, and ConversationHeader still
  // snoozes. If one is re-added as a chip, this is the test to change.
  it("offers no Unread, Snoozed or Overdue chip", () => {
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="open" onFilterChange={() => {}} />, client);

    for (const label of ["Unread", "Snoozed", "Overdue"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("keeps the widest scope reachable: All maps to filter=all, which includes snoozed", async () => {
    // "open" hides anything snoozed into the future (backend conversations.py), so with
    // the Snoozed chip gone this pill is the only way back to a snoozed conversation.
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="open" onFilterChange={onFilterChange} />, client);

    await user.click(screen.getByRole("button", { name: "All" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("all");
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
      <FilterChips chips={fourChips} filter="important" onFilterChange={() => {}} />,
      client,
    );

    for (const chip of fourChips) {
      expect(
        screen.getByRole("button", { name: chip.label }).getAttribute("aria-pressed"),
      ).toBe(chip.filter === "important" ? "true" : "false");
    }
  });

  it("toggles Unresponded on and back to open as a pill", async () => {
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChipsToggleHarness initialFilter="open" onFilterChange={onFilterChange} />,
      client,
    );

    await user.click(screen.getByRole("button", { name: "Unresponded" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("unresponded");

    // Pressing the active pill again is the only way back to the resting "open" scope
    // now that the Open/All dropdown is gone - so it has to work.
    await user.click(screen.getByRole("button", { name: "Unresponded" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("open");
  });

  it("toggles Important on and back to open as a pill", async () => {
    const user = userEvent.setup();
    const onFilterChange = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <FilterChipsToggleHarness initialFilter="open" onFilterChange={onFilterChange} />,
      client,
    );

    await user.click(screen.getByRole("button", { name: "Important" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("important");

    await user.click(screen.getByRole("button", { name: "Important" }));
    expect(onFilterChange).toHaveBeenLastCalledWith("open");
  });

  it("marks only the active chip with aria-pressed among the three defaults", () => {
    const client = makeStubClient({});
    renderWithProviders(<FilterChips filter="important" onFilterChange={() => {}} />, client);

    for (const chip of FILTER_CHIPS) {
      expect(
        screen.getByRole("button", { name: chip.label }).getAttribute("aria-pressed"),
      ).toBe(chip.filter === "important" ? "true" : "false");
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

  it("still names every filter value exactly once, including the ones with no chip", () => {
    // The Open/All dropdown is gone (the All chip replaced it), but FILTER_LABELS is what
    // stopped the old ternary chain from labelling two different filters "Unresponded" -
    // so the Record itself stays pinned. Every value ConversationFilter admits must still
    // have its own word.
    const filters: ConversationFilter[] = [
      "open",
      "unread",
      "unresponded",
      "important",
      "all",
      "snoozed",
      "overdue",
    ];
    for (const filter of filters) {
      expect(FILTER_LABELS[filter]).toBeTruthy();
    }
    expect(new Set(Object.values(FILTER_LABELS)).size).toBe(filters.length);
    expect(Object.keys(FILTER_LABELS).sort()).toEqual([...filters].sort());
  });

  it("renders no Open/All dropdown beside the pills", () => {
    const client = makeStubClient({});
    const view = renderWithProviders(
      <ConversationList
        items={[]}
        selectedContactE164={null}
        onSelect={() => {}}
        tab="chats"
        onTabChange={() => {}}
        filter="open"
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

    expect(view.container.querySelector('button[aria-haspopup="menu"]')).toBeNull();
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
