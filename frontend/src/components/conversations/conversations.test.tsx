import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "@/components/shell/Sidebar";
import { ConversationList } from "./ConversationList";
import { Timeline } from "./Timeline";
import { ContactPanel } from "./ContactPanel";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import type { Conversation, Inbox } from "@/api/conversations";

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
    unread: 0,
    status: "open",
    ...overrides,
  };
}

describe("Sidebar", () => {
  it("renders inboxes and an All inboxes entry for admins", async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [inbox()],
    });
    renderWithProviders(<Sidebar />, client);

    expect(await screen.findByText("Sales")).toBeInTheDocument();
    expect(screen.getByText("All inboxes")).toBeInTheDocument();
    expect(screen.getByText("(469) 461-7576")).toBeInTheDocument();
  });
});

describe("ConversationList", () => {
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
});

describe("Timeline", () => {
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

    renderWithProviders(<Timeline contactE164="+19725550199" ourE164="+14694617576" />, client);

    expect(await screen.findByText("hello")).toBeInTheDocument();
    expect(screen.getByText("Called you")).toBeInTheDocument();
    expect(screen.getByText("Call failed — carrier_unreachable")).toBeInTheDocument();
  });

  // F12
  it("shows a distinct empty state for a selected conversation with zero events", async () => {
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550199/timeline": { items: [], next_cursor: null },
    });

    renderWithProviders(<Timeline contactE164="+19725550199" ourE164="+14694617576" />, client);

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

    renderWithProviders(<Timeline contactE164="+19725550199" ourE164="+14694617576" />, client);

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
    attributes: { company: "Acme Inc", role: "Owner", email: "ada@example.com", address: "1 Main St" },
    notes: null,
    phones: [],
  };

  function renderPanel(contactsStub: RouteStub | typeof baseContact) {
    const client = makeStubClient({ "/api/v1/contacts/c1": contactsStub });
    renderWithProviders(
      <SoftphoneProvider>
        <ContactPanel conversation={conversation()} inbox={null} />
      </SoftphoneProvider>,
      client,
    );
    return client;
  }

  it("reads company, role, email, and address from the contact's attributes", async () => {
    renderPanel(baseContact);

    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
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
