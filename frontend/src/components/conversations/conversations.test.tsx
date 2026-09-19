import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationList } from "./ConversationList";
import { Timeline } from "./Timeline";
import { ContactPanel } from "./ContactPanel";
import { ConversationHeader } from "./ConversationHeader";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import type { Conversation, Inbox } from "@/api/conversations";
import { avatarHueIndex, shortRelativeTime } from "@/lib/format";

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

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    our_e164: "+14694617576",
    contact_e164: "+19725550199",
    inbox_id: "i1",
    thread_id: "t1",
    contact: { id: "c1", display_name: "Ada Lovelace" },
    snippet: "are you there?",
    last_event_type: "message",
    direction: "inbound",
    last_event_at: new Date().toISOString(),
    unread: false,
    status: "open",
    ...overrides,
  };
}

// P20a: the old 280px sidebar listed every inbox (name, colour dot, number) and an
// "All inboxes" entry. The new 56px rail carries navigation ONLY - the per-inbox list
// moves into the Inbox page's own column in P20b - so that assertion no longer describes
// anything that exists. The rail's own behaviour (which items render, permission gating,
// the org switcher) is covered by src/components/shell/Sidebar.test.tsx.

describe("ConversationList", () => {
  /** The list with nothing but `items` varying - every other prop is inert. */
  function renderList(items: Conversation[]) {
    const client = makeStubClient({});
    renderWithProviders(
      <ConversationList
        items={items}
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
  }

  // console-reference.html: coloured circles with two-letter initials, a different hue
  // per contact. The hue is a hash of the contact's id (avatarHueIndex), so it is the
  // SAME on every render and in every tab - that stability is the point of it.
  it("gives each contact two-letter initials and a hue derived from their id", () => {
    const items: Conversation[] = [
      conversation({ contact: { id: "c-ada", display_name: "Ada Whitlock" } }),
      conversation({
        contact_e164: "+19725550200",
        contact: { id: "c-marcus", display_name: "Marcus Bell" },
      }),
      conversation({ contact_e164: "+15125550177", contact: null, snippet: "STOP" }),
    ];
    renderList(items);

    const ada = screen.getByText("AW");
    const marcus = screen.getByText("MB");
    // No contact record: the formatted number is the title, and "51" its initials -
    // exactly the reference's grey "(512) 555-0177" row.
    const unsaved = screen.getByText("51");

    for (const avatar of [ada, marcus, unsaved]) {
      expect(avatar).toHaveClass("cx-avatar");
      expect(avatar.getAttribute("data-hue")).toMatch(/^[0-6]$/);
    }
    // Different contacts, different colours: a uniform list is the gap this closes.
    expect(ada.getAttribute("data-hue")).not.toBe(marcus.getAttribute("data-hue"));
    expect(ada.getAttribute("data-hue")).toBe(avatarHueIndex("c-ada").toString());
    // Not a saved contact, so the seed is the number rather than a contact id.
    expect(unsaved.getAttribute("data-hue")).toBe(avatarHueIndex("+15125550177").toString());
  });

  it("times rows in the reference's short relative form, including an old one", () => {
    const now = Date.now();
    renderList([
      conversation({ last_event_at: new Date(now - 12 * 60_000).toISOString() }),
      conversation({
        contact_e164: "+19725550200",
        last_event_at: new Date(now - 4 * 3_600_000).toISOString(),
      }),
      conversation({
        contact_e164: "+19725550201",
        // Old enough to be a date rather than a count - and the year is dropped, so it
        // reads "Sep 17", not "9/17/2025".
        last_event_at: new Date(now - 30 * 86_400_000).toISOString(),
      }),
    ]);

    expect(screen.getByText("12m")).toBeInTheDocument();
    expect(screen.getByText("4h")).toBeInTheDocument();
    expect(screen.getByText(shortRelativeTime(new Date(now - 30 * 86_400_000).toISOString())))
      .toBeInTheDocument();
    // The thing the operator actually complained about is gone.
    expect(screen.queryByText("now")).not.toBeInTheDocument();
  });

  it("marks an important row with a star and an unread row with a dot", () => {
    renderList([
      conversation({ important: true, unread: false }),
      conversation({ contact_e164: "+19725550200", important: false, unread: true }),
      conversation({ contact_e164: "+19725550201", important: false, unread: false }),
    ]);

    // One star, on the important row only.
    expect(screen.getAllByLabelText("Important")).toHaveLength(1);
    // One dot, on the unread row only. The dot has no text and no role - it is decoration
    // backing up the bold name - so it is queried by class.
    expect(document.querySelectorAll(".cx-unread-dot")).toHaveLength(1);
  });

  it("prefixes the preview with You: when the last event was ours", () => {
    renderList([
      conversation({ direction: "outbound", snippet: "Perfect, thank you!" }),
      conversation({
        contact_e164: "+19725550200",
        direction: "inbound",
        snippet: "Is the 0800 number included?",
      }),
    ]);

    const ours = screen.getByText("Perfect, thank you!");
    expect(within(ours).getByText("You:")).toBeInTheDocument();
    const theirs = screen.getByText("Is the 0800 number included?");
    expect(within(theirs).queryByText("You:")).not.toBeInTheDocument();
  });

  it("renders message, missed call, and voicemail snippets with icons", () => {
    const client = makeStubClient({});
    const items: Conversation[] = [
      conversation({ snippet: "hello there", last_event_type: "message" }),
      conversation({
        thread_id: "t2",
        contact_e164: "+19725550200",
        snippet: "Missed call",
        last_event_type: "call",
        direction: "inbound",
      }),
      conversation({
        thread_id: "t3",
        contact_e164: "+19725550201",
        snippet: "New voicemail",
        last_event_type: "voicemail",
      }),
    ];
    renderWithProviders(
      <ConversationList
        items={items}
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

    expect(screen.getByText("hello there")).toBeInTheDocument();
    expect(screen.getByText("Missed call")).toBeInTheDocument();
    expect(screen.getByText("New voicemail")).toBeInTheDocument();
    // All three rows share the default fixture's contact ("Ada Lovelace").
    expect(screen.getAllByText("Ada Lovelace")).toHaveLength(3);
  });

  it("shows the no-access empty state", () => {
    const client = makeStubClient({});
    renderWithProviders(
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
        hasNoInboxAccess
      />,
      client,
    );
    expect(
      screen.getByText("You have no inbox access yet — ask an admin"),
    ).toBeInTheDocument();
  });

  // Items 11/12/36: call-only conversations have no thread yet - two of them (distinct
  // contacts, both thread_id: null) must render as two rows, not collide/dedupe on a
  // shared React key, and a null direction must not blow up the icon.
  it("renders two null-thread_id rows distinctly and a null direction neutrally", () => {
    const client = makeStubClient({});
    const items: Conversation[] = [
      conversation({
        thread_id: null,
        contact_e164: "+19725550200",
        snippet: "call only, no thread",
        last_event_type: "call",
        direction: null,
      }),
      conversation({
        thread_id: null,
        contact_e164: "+19725550201",
        snippet: "also call only",
        last_event_type: "call",
        direction: null,
      }),
    ];
    renderWithProviders(
      <ConversationList
        items={items}
        selectedContactE164={null}
        onSelect={() => {}}
        tab="calls"
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

    expect(screen.getByText("call only, no thread")).toBeInTheDocument();
    expect(screen.getByText("also call only")).toBeInTheDocument();
  });

  // Item 2: Important filter chip + starred row indicator.
  it("shows a star on important rows and toggles the Important filter chip", async () => {
    const client = makeStubClient({});
    const onFilterChange = vi.fn();
    const items: Conversation[] = [
      conversation({ important: true }),
      conversation({ contact_e164: "+19725550200", contact: { id: "c2", display_name: "Bob Bond" } }),
    ];
    renderWithProviders(
      <ConversationList
        items={items}
        selectedContactE164={null}
        onSelect={() => {}}
        tab="chats"
        onTabChange={() => {}}
        filter="open"
        onFilterChange={onFilterChange}
        q=""
        onQChange={() => {}}
        hasNextPage={false}
        isFetchingNextPage={false}
        isLoading={false}
        onLoadMore={() => {}}
      />,
      client,
    );

    expect(screen.getAllByLabelText("Important")).toHaveLength(1);

    // The chips are three plain pills again (console-reference.html), so Important is a
    // button - not an item inside a collapsed "Filter" dropdown. The row's star carries
    // aria-label="Important" too, so query by ROLE to keep hold of the chip.
    await userEvent.click(screen.getByRole("button", { name: "Important" }));
    expect(onFilterChange).toHaveBeenCalledWith("important");
  });

  // Item 1, redrawn: the reference's two `.icon-btn`s at the right of the list header - a
  // phone and a speech bubble - which replaced the "+ New" dropdown. Both destinations
  // are still reachable, and both still carry the compose gate.
  it("fires onNew from each header icon button, and both are disabled when canCompose is false", async () => {
    const client = makeStubClient({});
    const onNew = vi.fn();
    const { rerender } = renderWithProviders(
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
        onNew={onNew}
        canCompose
      />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "New text message" }));
    expect(onNew).toHaveBeenCalledWith("message");

    await userEvent.click(screen.getByRole("button", { name: "New call" }));
    expect(onNew).toHaveBeenCalledWith("call");

    rerender(
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
        onNew={onNew}
        canCompose={false}
      />,
    );
    for (const name of ["New call", "New text message"]) {
      const disabledButton = screen.getByRole("button", { name });
      expect(disabledButton).toBeDisabled();
      expect(disabledButton).toHaveAttribute(
        "title",
        "Read-only inbox — you can view but not start new conversations",
      );
    }
  });

  // Item 6: the disabled tooltip must not claim "read-only" while we don't yet know
  // whether the user can compose (inboxes query still in flight).
  it("shows no tooltip on the disabled header buttons while canComposeLoading is true", () => {
    const client = makeStubClient({});
    renderWithProviders(
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
        onNew={() => {}}
        canCompose={false}
        canComposeLoading
      />,
      client,
    );
    for (const name of ["New call", "New text message"]) {
      const newButton = screen.getByRole("button", { name });
      expect(newButton).toBeDisabled();
      expect(newButton).not.toHaveAttribute("title");
    }
  });
});

describe("Timeline", () => {
  const TIMELINE = "/api/v1/conversations/%2B19725550199/timeline";

  function message(overrides: Record<string, unknown> = {}) {
    return {
      kind: "message",
      id: "m1",
      direction: "inbound",
      body: "hello",
      media: null,
      status: "received",
      occurred_at: new Date().toISOString(),
      error_code: null,
      route_reason: null,
      failure_reason_public: null,
      scheduled_for: null,
      clicks: 0,
      links: [],
      ...overrides,
    };
  }

  function renderTimeline(items: unknown[]) {
    const client = makeStubClient({ [TIMELINE]: { items, next_cursor: null } });
    renderWithProviders(
      <SoftphoneProvider>
        <Timeline
          contactE164="+19725550199"
          ourE164="+14694617576"
          contactName="Priya Raman"
          inboxName="Main line"
        />
      </SoftphoneProvider>,
      client,
    );
    return client;
  }

  // console-reference.html `.day`: a centred pill, not a rule with a label in it.
  it("draws the day divider as a centred pill, named by weekday when it is recent", async () => {
    renderTimeline([message()]);

    const pill = await screen.findByText("Today");
    expect(pill).toHaveClass("cx-daybreak");
    // `cx-label` was the 9px uppercase mono of the old rule - it cannot live in a pill.
    expect(pill).not.toHaveClass("cx-label");
  });

  // console-reference.html `.bubble` / `.grp.out .bubble`: theirs recessed, ours azure.
  it("gives an inbound bubble the recessed class and an outbound bubble the azure one", async () => {
    renderTimeline([
      message({ id: "m1", direction: "inbound", body: "from them" }),
      message({ id: "m2", direction: "outbound", body: "from us", status: "delivered" }),
    ]);

    const theirs = (await screen.findByText("from them")).closest(".cx-msg");
    const ours = screen.getByText("from us").closest(".cx-msg");
    expect(theirs).toHaveClass("cx-msg-in");
    expect(theirs).not.toHaveClass("cx-msg-out");
    expect(ours).toHaveClass("cx-msg-out");
    expect(ours).not.toHaveClass("cx-msg-in");
  });

  // `.grp-who`: the name above a run, once, and the time under the run, once.
  it("names each run once and times it once, with a tick on a delivered outbound run", async () => {
    renderTimeline([
      message({ id: "m1", direction: "inbound", body: "first" }),
      message({ id: "m2", direction: "inbound", body: "second" }),
      message({ id: "m3", direction: "outbound", body: "reply", status: "delivered" }),
    ]);

    await screen.findByText("first");
    // Two inbound messages, ONE name above them - and the outbound run is labelled by the
    // line it went out on, which is the only sender the API actually tells us.
    expect(screen.getAllByText("Priya Raman")).toHaveLength(1);
    expect(screen.getAllByText("Main line")).toHaveLength(1);
    // The first of the inbound run carries no time; only its last does.
    expect(document.querySelectorAll(".cx-meta")).toHaveLength(2);
    expect(screen.getByLabelText("Delivered")).toBeInTheDocument();
  });

  // console-reference.html `.callcard`: a full-width card, an arrow, "Outbound call ·
  // 6:12", and a Play recording button.
  it("renders a call as a full-width card with its length and a play button", async () => {
    renderTimeline([
      {
        kind: "call",
        id: "call1",
        direction: "outbound",
        status: "completed",
        duration_seconds: 372,
        occurred_at: new Date().toISOString(),
        answered_at: null,
        ended_at: null,
        failure_detail: null,
        recording: { id: "rec1", status: "stored", duration_seconds: 372 },
        has_voicemail: false,
      },
    ]);

    const line = await screen.findByText("You called · 6:12");
    const card = line.closest(".cx-callcard");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByRole("button", { name: /Play recording/ }))
      .toBeInTheDocument();
    // Not a bubble: a call belongs to neither side, so it is never aligned to a speaker.
    expect(card).not.toHaveClass("cx-msg-out");
    expect(card).not.toHaveClass("cx-msg-in");
  });

  it("renders a message bubble, call card, and failed call with failure detail", async () => {
    const client = makeStubClient({
      // fetchConversationTimeline() URL-encodes the contact E.164 (encodeURIComponent
      // turns "+" into "%2B") - the stub key has to match the actual request path.
      "/api/v1/conversations/%2B19725550199/timeline": {
        items: [
          {
            kind: "message",
            id: "m1",
            direction: "inbound",
            body: "hello",
            media: null,
            status: "received",
            occurred_at: new Date().toISOString(),
            error_code: null,
          },
          {
            kind: "call",
            id: "call1",
            direction: "inbound",
            status: "completed",
            duration_seconds: 42,
            occurred_at: new Date().toISOString(),
            answered_at: null,
            ended_at: null,
            failure_detail: null,
            recording: null,
            has_voicemail: false,
          },
          {
            kind: "call",
            id: "call2",
            direction: "outbound",
            status: "failed",
            duration_seconds: null,
            occurred_at: new Date().toISOString(),
            answered_at: null,
            ended_at: null,
            failure_detail: "carrier_unreachable",
            recording: null,
            has_voicemail: false,
          },
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+14694617576" />
      </SoftphoneProvider>,
      client,
    );

    expect(await screen.findByText("hello")).toBeInTheDocument();
    // The card now carries the direction and the length on one line, as the reference's
    // "Outbound call · 6:12" does.
    expect(screen.getByText("Called you · 0:42")).toBeInTheDocument();
    expect(screen.getByText("Call failed — carrier_unreachable")).toBeInTheDocument();
  });

  // P21: a non-null route_reason (backend MessageOut/CallOut.route_reason) renders as a
  // tooltip on the bubble/call card plus sr-only text for screen readers - layout is
  // otherwise unchanged. A null reason must add neither.
  it("shows the route reason as a tooltip and sr-only text on a message and a call, and nothing when null", async () => {
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550199/timeline": {
        items: [
          {
            kind: "message",
            id: "m2",
            direction: "outbound",
            body: "on our way",
            media: null,
            status: "sent",
            occurred_at: new Date().toISOString(),
            error_code: null,
            route_reason: "Sent via Telnyx — cheapest healthy route",
          },
          {
            kind: "call",
            id: "call4",
            direction: "outbound",
            status: "completed",
            duration_seconds: 30,
            occurred_at: new Date().toISOString(),
            answered_at: null,
            ended_at: null,
            failure_detail: null,
            recording: null,
            has_voicemail: false,
            route_reason: "Failed over to Telnyx — Bandwidth unavailable",
          },
          {
            kind: "message",
            id: "m3",
            direction: "inbound",
            body: "no reason here",
            media: null,
            status: "received",
            occurred_at: new Date().toISOString(),
            error_code: null,
            route_reason: null,
          },
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+14694617576" />
      </SoftphoneProvider>,
      client,
    );

    const bubble = (await screen.findByText("on our way")).closest("[title]");
    expect(bubble).toHaveAttribute("title", "Sent via Telnyx — cheapest healthy route");
    expect(
      within(bubble as HTMLElement).getByText("Sent via Telnyx — cheapest healthy route"),
    ).toBeInTheDocument();

    const callCard = screen.getByText("You called · 0:30").closest("[title]");
    expect(callCard).toHaveAttribute("title", "Failed over to Telnyx — Bandwidth unavailable");
    expect(
      within(callCard as HTMLElement).getByText("Failed over to Telnyx — Bandwidth unavailable"),
    ).toBeInTheDocument();

    expect(screen.getByText("no reason here").closest("[title]")).toBeNull();
  });

  // F12
  it("shows a distinct empty state for a selected conversation with zero events", async () => {
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550199/timeline": { items: [], next_cursor: null },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+14694617576" />
      </SoftphoneProvider>,
      client,
    );

    expect(await screen.findByText("No messages or calls yet")).toBeInTheDocument();
  });

  // F11
  it("renders a Play recording button with duration for a call that has one", async () => {
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550199/timeline": {
        items: [
          {
            kind: "call",
            id: "call3",
            direction: "outbound",
            status: "completed",
            duration_seconds: 12,
            occurred_at: new Date().toISOString(),
            answered_at: null,
            ended_at: null,
            failure_detail: null,
            recording: { id: "rec1", status: "stored", duration_seconds: 12 },
            has_voicemail: false,
          },
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+14694617576" />
      </SoftphoneProvider>,
      client,
    );

    expect(
      await screen.findByRole("button", { name: "Play recording" }),
    ).toBeInTheDocument();
  });
});

describe("InboxSettingsPage grant editor round-trip", () => {
  it("saves grants via PUT with the expected body", async () => {
    const client = makeStubClient({
      // The stub matcher is startsWith-based - the specific "/inboxes/i1/grants" route
      // must be listed (and therefore found) before the general "/inboxes" one, or every
      // grants request would incorrectly resolve to the inbox list instead.
      "/api/v1/inboxes/i1/grants": [
        { grantee_type: "user", grantee_id: "u1", role: "member" },
      ],
      "/api/v1/inboxes": [inbox()],
      "/api/v1/departments": [
        { id: "d1", name: "Support", is_active: true, member_user_ids: [] },
      ],
      "/api/v1/orgs/current/members": [
        { user_id: "u1", full_name: "Charlie", email: "charlie@example.com", role_name: "agent" },
      ],
    });

    const { InboxSettingsPage } = await import("@/pages/InboxSettingsPage");
    renderWithProviders(<InboxSettingsPage />, client);

    const saveButton = await screen.findByRole("button", { name: "Save grants" });
    await userEvent.click(saveButton);

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/inboxes/i1/grants" &&
            call.init.method === "PUT",
        ),
      ).toBe(true),
    );
    const putCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/inboxes/i1/grants" &&
        call.init.method === "PUT",
    );
    expect(putCall?.init.json).toEqual({
      grants: [{ grantee_type: "user", grantee_id: "u1", role: "member" }],
    });
  });
});

describe("ContactPanel", () => {
  const baseContact = {
    id: "c1",
    display_name: "Ada Lovelace",
    // Typed loosely on purpose: the backend's `attributes` is free-form JSON, and tests
    // below stub a contact carrying only SOME of these keys.
    attributes: {
      company: "Acme Inc",
      role: "Owner",
      email: "ada@example.com",
      address: "1 Main St",
    } as Record<string, unknown>,
    notes: null,
    phones: [],
  };

  // `Collapsible` persists its open/closed state to localStorage, which jsdom shares
  // across tests in this file - a test that opens "Details" would otherwise leave it
  // open for the next one and make the "these are not on the panel" assertions lie.
  beforeEach(() => {
    localStorage.removeItem("contact-panel.details");
    localStorage.removeItem("contact-panel.notes");
  });

  function renderPanel(contactsStub: RouteStub | typeof baseContact) {
    const client = makeStubClient({
      // The more specific "/notes" route must be listed (and therefore matched) before
      // the general "/api/v1/contacts/c1" one below - the stub matcher is startsWith-based.
      "/api/v1/contacts/c1/notes": [],
      "/api/v1/contacts/c1": contactsStub,
    });
    renderWithProviders(
      <SoftphoneProvider>
        <ContactPanel conversation={conversation()} inbox={null} />
      </SoftphoneProvider>,
      client,
    );
    return client;
  }

  // NOTE on all of these: vitest runs with `css: false`, so the stylesheet is never
  // applied. None of these assertions proves a colour, a corner, a gradient or a type
  // size - they prove which elements and which classes are in the DOM. `.cx-panel-name`
  // being LARGER than body text is a claim only the browser can settle.

  // console-reference.html's panel: a big avatar, the name, and exactly Phone / Email /
  // Company as `.f` rows - not folded inside a "Details" disclosure the way they were.
  it("leads with the name and the Phone, Email and Company rows", async () => {
    renderPanel(baseContact);

    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    for (const key of ["Phone", "Email", "Company"]) {
      const row = screen.getByText(key).closest(".cx-field");
      expect(row).not.toBeNull();
    }
    // FOUR things: the name plus exactly three field rows. A fourth `.cx-field` creeping
    // back in is the regression this guards.
    const panel = screen.getByLabelText("Contact panel");
    expect(panel.querySelectorAll(".cx-field")).toHaveLength(3);
    expect(
      Array.from(panel.querySelectorAll(".cx-field-k")).map((el) => el.textContent),
    ).toEqual(["Phone", "Email", "Company"]);

    // The name is the panel's heading, carried WITHOUT a "NAME" label above it.
    expect(screen.getByRole("button", { name: "Edit Name" })).toHaveTextContent(
      "Ada Lovelace",
    );
    expect(panel.querySelector(".cx-panel-name")).not.toBeNull();
    expect(screen.queryByText("Name")).not.toBeInTheDocument();

    // The avatar carries this contact's own hue, from the same helper the list uses.
    const avatar = screen.getByText("AL");
    expect(avatar).toHaveClass("cx-avatar");
    expect(avatar).toHaveClass("cx-panel-av");
    expect(avatar.getAttribute("data-hue")).toBe(avatarHueIndex("c1").toString());

    // The contact's number is in the Phone row, formatted by `formatPhone`, not raw E.164.
    const phoneRow = screen.getByText("Phone").closest(".cx-field") as HTMLElement;
    expect(within(phoneRow).getByText("(972) 555-0199")).toBeInTheDocument();
    expect(within(phoneRow).queryByText("+19725550199")).not.toBeInTheDocument();
  });

  // The operator's instruction, given twice: four things. These five were on the panel
  // and are not any more. Paired with the reachability assertion at the end so the
  // absence is not vacuous - a panel that rendered nothing at all would also "pass"
  // every queryByText below.
  it("drops Owner, Team, Role, Address and the note list, and still routes to the full record", async () => {
    renderPanel(baseContact);
    await screen.findByText("Acme Inc");

    const panel = screen.getByLabelText("Contact panel");
    // Substring match on the panel's whole text, NOT getByText - getByText compares the
    // normalised text of one element, so a re-added `<p>Owner: Unassigned</p>` would
    // match neither "Owner:" nor "Unassigned" and the assertion would pass while the row
    // was back on screen. (It did, the first time this test was written.)
    const text = panel.textContent ?? "";
    for (const gone of ["Owner", "Team", "Unassigned", "No team", "Open contact"]) {
      expect(text).not.toContain(gone);
    }
    // Role's value here is "Owner" and Address's is "1 Main St"; neither is on the face
    // of the panel, and the note composer is not either - both disclosures are shut.
    expect(text).not.toContain("1 Main St");
    expect(within(panel).queryByLabelText("Add note")).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Edit Role" })).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", { name: "Edit Address" }),
    ).not.toBeInTheDocument();

    // ...and the way to Owner/Team/duplicates/export is still here: the avatar links to
    // the contact page. Without this the assertions above would pass on an empty panel.
    const link = within(panel).getByRole("link", {
      name: "Open contact record for Ada Lovelace",
    });
    expect(link).toHaveAttribute("href", "/contacts/c1");
    expect(within(link).getByText("AL")).toHaveClass("cx-avatar");
  });

  // Stable shape: an absent Email still draws its row, with a muted em-dash, rather than
  // the panel being two rows tall for one contact and three for the next.
  it("draws the Email row with an em-dash when the contact has no email", async () => {
    renderPanel({ ...baseContact, attributes: { company: "Acme Inc" } });

    await screen.findByText("Acme Inc");
    const panel = screen.getByLabelText("Contact panel");
    expect(panel.querySelectorAll(".cx-field")).toHaveLength(3);

    const emailRow = screen.getByText("Email").closest(".cx-field") as HTMLElement;
    expect(within(emailRow).getByText("—")).toBeInTheDocument();
    // "Add" was the link the approved design does not have; the row is still editable.
    expect(within(emailRow).queryByText("Add")).not.toBeInTheDocument();
    expect(within(emailRow).getByRole("button", { name: "Edit Email" })).toBeInTheDocument();
  });

  it("closes from its own X when the caller can close it", async () => {
    const onClose = vi.fn();
    const client = makeStubClient({
      "/api/v1/contacts/c1/notes": [],
      "/api/v1/contacts/c1": baseContact,
    });
    renderWithProviders(
      <SoftphoneProvider>
        <ContactPanel conversation={conversation()} inbox={null} onClose={onClose} />
      </SoftphoneProvider>,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Close contact panel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("reads company, role, email, and address from the contact's attributes", async () => {
    renderPanel(baseContact);

    // Company and Email are on the face of the panel...
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();

    // ...Role and Address are one click away, in the disclosure that exists only because
    // /contacts/:contactId cannot render either of them. Deleting them would delete the
    // feature, so they are still here and still editable.
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("1 Main St")).toBeInTheDocument();
  });

  it("saves an edit via PATCH with the full merged attributes, and shows a saved state", async () => {
    const client = renderPanel((_path, init) => {
      if (init.method === "PATCH") {
        const body = init.json as { attributes: Record<string, unknown> };
        return { ...baseContact, attributes: body.attributes };
      }
      return baseContact;
    });

    await userEvent.click(await screen.findByText("Acme Inc"));
    const input = screen.getByLabelText("Company");
    await userEvent.clear(input);
    await userEvent.type(input, "New Co");
    await userEvent.click(screen.getByRole("button", { name: "Save Company" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/contacts/c1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );
    const patchCall = client.calls.find(
      (call) => call.path === "/api/v1/contacts/c1" && call.init.method === "PATCH",
    );
    // The unrelated attributes (role/email/address) must survive the round-trip - the
    // backend replaces `attributes` wholesale, so a partial PATCH would silently wipe them.
    expect(patchCall?.init.json).toEqual({
      attributes: {
        company: "New Co",
        role: "Owner",
        email: "ada@example.com",
        address: "1 Main St",
      },
    });
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("shows an error and keeps the field open (no silent failure) when the save fails", async () => {
    renderPanel((_path, init) => {
      if (init.method === "PATCH") {
        return new Error("Unknown custom field: 'role'");
      }
      return baseContact;
    });

    await screen.findByText("Acme Inc");
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    await userEvent.click(await screen.findByText("Owner"));
    const input = screen.getByLabelText("Role");
    await userEvent.clear(input);
    await userEvent.type(input, "Manager");
    await userEvent.click(screen.getByRole("button", { name: "Save Role" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unknown custom field: 'role'",
    );
    // Still editing, with the user's typed value intact - the failed save never reverted
    // or silently discarded it.
    expect(screen.getByLabelText("Role")).toHaveValue("Manager");
  });

  // Item 3
  it("lists existing notes and adds a new one via POST /contacts/{id}/notes", async () => {
    const existingNote = {
      id: "note-1",
      body: "Called back, left voicemail",
      author_user_id: "u1",
      created_at: new Date().toISOString(),
    };
    const client = makeStubClient({
      "/api/v1/contacts/c1/notes": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          const body = init.json as { body: string };
          return { id: "note-2", body: body.body, author_user_id: "u1", created_at: new Date().toISOString() };
        }
        return [existingNote];
      },
      "/api/v1/contacts/c1": baseContact,
    });
    renderWithProviders(
      <SoftphoneProvider>
        <ContactPanel conversation={conversation()} inbox={null} />
      </SoftphoneProvider>,
      client,
    );

    // Notes are behind a disclosure now: the panel's face is the four things the design
    // asks for, but this panel is still the ONLY place in the app that can read or add a
    // contact note, so the capability is a click away rather than gone.
    await userEvent.click(await screen.findByRole("button", { name: "Notes" }));
    expect(await screen.findByText("Called back, left voicemail")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Add note"), "Sent the contract");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/contacts/c1/notes" && call.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (call) => call.path === "/api/v1/contacts/c1/notes" && call.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({ body: "Sent the contract" });
    // The composer clears once the note is saved.
    await waitFor(() => expect(screen.getByLabelText("Add note")).toHaveValue(""));
  });
});

describe("ConversationHeader", () => {
  // Item 11/12
  it("disables Close/Reopen for a call-only conversation with no thread yet", async () => {
    const client = makeStubClient({});
    renderWithProviders(
      <SoftphoneProvider>
        <ConversationHeader conversation={conversation({ thread_id: null })} />
      </SoftphoneProvider>,
      client,
    );

    // P20b: the header subtitle now carries a PhoneNumberMenu, which is a SECOND
    // aria-expanded button - target the "more" trigger by its accessible name instead.
    await userEvent.click(screen.getByRole("button", { name: "Conversation actions" }));
    const toggleButton = await screen.findByRole("menuitem");
    expect(toggleButton).toBeDisabled();
    expect(toggleButton).toHaveAttribute(
      "title",
      "This conversation has no messages yet — nothing to close or reopen",
    );
  });

  // Item 3: star toggle -> POST /api/v1/inbox/important-pair.
  it("marks a conversation as important via the star toggle", async () => {
    const client = makeStubClient({
      "/api/v1/inbox/important-pair": () => undefined,
    });
    renderWithProviders(
      <SoftphoneProvider>
        <ConversationHeader conversation={conversation({ important: false })} />
      </SoftphoneProvider>,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Mark as important" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/inbox/important-pair" && call.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find((call) => call.path === "/api/v1/inbox/important-pair");
    expect(postCall?.init.json).toEqual({
      our_e164: "+14694617576",
      contact_e164: "+19725550199",
      important: true,
    });
  });

  it("disables the star toggle for a read-only (canSend=false) conversation", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <SoftphoneProvider>
        <ConversationHeader conversation={conversation()} canSend={false} />
      </SoftphoneProvider>,
      client,
    );

    expect(screen.getByRole("button", { name: "Mark as important" })).toBeDisabled();
  });

  it("shows an inline error and does not crash when the star toggle fails", async () => {
    const client = makeStubClient({
      "/api/v1/inbox/important-pair": () => new Error("not visible"),
    });
    renderWithProviders(
      <SoftphoneProvider>
        <ConversationHeader conversation={conversation({ important: false })} />
      </SoftphoneProvider>,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Mark as important" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not visible");
  });
});

describe("InboxSettingsPage access gate", () => {
  it('shows "Admins only" when no inbox grants the admin role', async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [inbox({ my_role: "member" })],
    });
    const { InboxSettingsPage } = await import("@/pages/InboxSettingsPage");
    renderWithProviders(<InboxSettingsPage />, client);

    expect(await screen.findByText("Admins only")).toBeInTheDocument();
  });

  // F16
  it('shows an error, never "Admins only", when the inboxes fetch fails', async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": new Error("network down"),
    });
    const { InboxSettingsPage } = await import("@/pages/InboxSettingsPage");
    renderWithProviders(<InboxSettingsPage />, client);

    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
    expect(screen.queryByText("Admins only")).not.toBeInTheDocument();
  });
});
