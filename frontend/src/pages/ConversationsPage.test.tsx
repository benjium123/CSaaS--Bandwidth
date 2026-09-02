import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ApiClient } from "@/api/client";
import { ConversationsPage } from "./ConversationsPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { AuthProvider } from "@/auth/AuthContext";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import type { Inbox } from "@/api/conversations";

/** Minimal fake livekit-client Room - only what SoftphoneProvider.joinRoom() touches
 * (connect/localParticipant/on/disconnect). The dial() test only needs the outgoing POST
 * /api/v1/calls request to have happened - it never asserts on room state - but a real
 * Room would attempt a genuine WebSocket signal connection to the fake wss:// url and
 * either hang or spam errors, the same reason SoftphonePanel.test.tsx mocks it. */
const { FakeRoom } = vi.hoisted(() => {
  class FakeRoom {
    localParticipant = { setMicrophoneEnabled: async () => undefined };
    on() {
      return this;
    }
    off() {
      return this;
    }
    async connect() {
      /* no-op */
    }
    async disconnect() {
      /* no-op */
    }
  }
  return { FakeRoom };
});

vi.mock("livekit-client", () => ({
  Room: FakeRoom,
  RoomEvent: { TrackSubscribed: "trackSubscribed", ConnectionStateChanged: "ccs", Disconnected: "disconnected", MediaDevicesError: "mde" },
  ConnectionState: { Disconnected: "disconnected", Connecting: "connecting", Connected: "connected", Reconnecting: "reconnecting", SignalReconnecting: "sr" },
  Track: { Kind: { Audio: "audio", Video: "video" } },
  LocalParticipant: class {},
}));

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

// fetchConversationTimeline() URL-encodes the contact E.164 (encodeURIComponent turns
// "+" into "%2B") - the stub key has to match the actual request path, not the raw number.
const TIMELINE_PATH = "/api/v1/conversations/%2B19725550199/timeline";

function routes() {
  return {
    "/api/v1/inboxes": [inbox()],
    // The stub matcher is startsWith-based - the specific timeline route must be listed
    // (and therefore found) before the general "/conversations" route below, or every
    // timeline request would incorrectly resolve to the conversation list instead.
    [TIMELINE_PATH]: {
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
      ],
      next_cursor: null,
    },
    "/api/v1/conversations": {
      items: [
        {
          our_e164: "+14694617576",
          contact_e164: "+19725550199",
          inbox_id: "i1",
          thread_id: "t1",
          contact: { id: "c1", display_name: "Ada Lovelace" },
          snippet: "hello there",
          last_event_type: "message",
          direction: "inbound",
          last_event_at: new Date().toISOString(),
          unread: 2,
          status: "open",
        },
      ],
      next_cursor: null,
    },
    "/api/v1/contacts/c1": {
      id: "c1",
      display_name: "Ada Lovelace",
      attributes: { company: null, role: null, email: null, address: null },
      notes: null,
      phones: [],
    },
  };
}

// ConversationHeader and ContactPanel both call useSoftphone() (the "Call" button), so
// ConversationsPage - unlike a bare ConversationList/Timeline unit test - always needs a
// real SoftphoneProvider ancestor, the same way SoftphonePanel.test.tsx wraps it.
function renderPage(client: ApiClient) {
  return renderWithProviders(
    <SoftphoneProvider>
      <ConversationsPage />
    </SoftphoneProvider>,
    client,
  );
}

/** Same composition as test/harness.tsx's renderWithProviders, but with a specific
 * starting route - needed for the ?inbox=all case, and renderWithProviders itself always
 * starts a bare MemoryRouter with no initialEntries. */
function renderPageAt(path: string, client: ApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <SoftphoneProvider>
            <ConversationsPage />
          </SoftphoneProvider>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("ConversationsPage", () => {
  // ConversationsPage only owns the list/timeline/contact-panel columns - the app Shell
  // (frontend/src/App.tsx) mounts the one persistent <Sidebar />, so it is covered by the
  // "Sidebar" describe block in components/conversations/conversations.test.tsx instead
  // of being asserted on here.
  it("renders the conversation list, timeline, and composer", async () => {
    const client = makeStubClient(routes());
    renderPage(client);

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("hello there")).toBeInTheDocument();

    // The timeline and composer only render once a conversation is selected.
    await userEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));

    expect(await screen.findByText("hello")).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
  });

  it("shows no-access empty state when there are no inboxes", async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [],
      "/api/v1/conversations": { items: [], next_cursor: null },
    });
    renderPage(client);

    expect(
      await screen.findByText("You have no inbox access yet — ask an admin"),
    ).toBeInTheDocument();
  });

  it("renders failed-call failure_detail in the timeline", async () => {
    const client = makeStubClient({
      ...routes(),
      [TIMELINE_PATH]: {
        items: [
          {
            kind: "call",
            id: "call1",
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
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    expect(
      await screen.findByText("Call failed — carrier_unreachable"),
    ).toBeInTheDocument();
  });

  it("highlights selected conversation row", async () => {
    const client = makeStubClient(routes());
    renderPage(client);

    const rowButton = await screen.findByRole("button", { name: /Ada Lovelace/ });
    await userEvent.click(rowButton);
    expect(rowButton).toHaveAttribute("aria-current", "true");
  });

  it("sends a message through the existing Composer", async () => {
    const client = makeStubClient(routes());
    renderPage(client);

    // The Composer only mounts once a conversation is selected.
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    const input = await screen.findByLabelText("Message");
    await userEvent.type(input, "new message");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/messages" &&
            call.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/messages" &&
        call.init.method === "POST",
    );
    // F3: the send body must include `from` (the conversation own our_e164) - relying
    // on the server sticky-sender default silently picked whatever number it wanted.
    expect(postCall?.init.json).toEqual({
      to: "+19725550199",
      from: "+14694617576",
      body: "new message",
      allow_reassign: false,
    });
  });

  // F1/F2
  it("disables the composer and Call buttons for a viewer-role inbox, with a read-only hint", async () => {
    const client = makeStubClient({
      ...routes(),
      "/api/v1/inboxes": [inbox({ my_role: "viewer" })],
    });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    expect(
      await screen.findByText("Read-only inbox — you can view but not send"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    // Both the header's and the contact panel's Call button must be gated.
    const callButtons = screen.getAllByRole("button", { name: /Call Ada Lovelace/ });
    expect(callButtons.length).toBeGreaterThan(0);
    callButtons.forEach((button) => expect(button).toBeDisabled());
  });

  it("maps tab, filter, and the debounced search box onto the conversations request", async () => {
    const client = makeStubClient(routes());
    renderPage(client);
    await screen.findByText("Ada Lovelace");

    await userEvent.click(screen.getByRole("tab", { name: "Calls" }));
    await userEvent.click(screen.getByRole("button", { name: "Unread" }));
    await userEvent.type(screen.getByLabelText("Search conversations"), "ada");

    // F20: the search box is debounced 300ms before it reaches the query - give it room.
    await waitFor(
      () => {
        const call = client.calls.find(
          (c) => c.path.startsWith("/api/v1/conversations?") && c.path.includes("q=ada"),
        );
        expect(call).toBeDefined();
      },
      { timeout: 2000 },
    );

    const call = client.calls.find(
      (c) => c.path.startsWith("/api/v1/conversations?") && c.path.includes("q=ada"),
    );
    expect(call?.path).toBe("/api/v1/conversations?inbox_id=i1&tab=calls&filter=unread&q=ada");
  });

  it("calls softphone.dial with the conversation's contact_e164 and our_e164", async () => {
    const client = makeStubClient({
      ...routes(),
      "/api/v1/calls": ((_path, init) => {
        if (init.method === "POST") {
          return {
            id: "call-9",
            contact_e164: "+19725550199",
            room: "room-9",
            token: "tok-9",
            url: "wss://lk.example.com",
          };
        }
        throw new Error("unexpected request");
      }) as RouteStub,
    });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    const [callButton] = await screen.findAllByRole("button", { name: /Call Ada Lovelace/ });
    await userEvent.click(callButton);

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/calls" && c.init.method === "POST"),
      ).toBe(true),
    );
    const dialCall = client.calls.find(
      (c) => c.path === "/api/v1/calls" && c.init.method === "POST",
    );
    expect(dialCall?.init.json).toEqual({
      to: "+19725550199",
      from: "+14694617576",
      via: "room",
    });
  });

  // F7
  it("?inbox=all queries every inbox without an inbox_id filter and skips auto-select", async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [
        inbox({ id: "i1", name: "Sales" }),
        inbox({ id: "i2", name: "Support", e164: "+12145550111", number_id: "n2" }),
      ],
      "/api/v1/conversations": { items: [], next_cursor: null },
    });
    renderPageAt("/inbox?inbox=all", client);

    expect(await screen.findByText("No conversations yet")).toBeInTheDocument();

    const conversationsCall = client.calls.find(
      (c) => c.path.startsWith("/api/v1/conversations") && !c.path.includes("timeline"),
    );
    expect(conversationsCall?.path).not.toContain("inbox_id");
  });

  // Follow-up (4): in ?inbox=all mode, the conversation's OWN inbox governs canSend -
  // not inboxes[0] - so a viewer-role inbox still gates its conversations even when it
  // is not the first inbox in the list.
  it("?inbox=all: composer stays disabled for a conversation whose own inbox is viewer-role, even when that inbox isn't inboxes[0]", async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [
        inbox({ id: "i1", name: "Sales", my_role: "admin" }),
        inbox({
          id: "i2",
          name: "Support",
          e164: "+12145550111",
          number_id: "n2",
          my_role: "viewer",
        }),
      ],
      // The stub matcher is startsWith-based - the specific timeline route must be
      // listed (and therefore found) before the general "/conversations" route below.
      "/api/v1/conversations/%2B19725550250/timeline": { items: [], next_cursor: null },
      "/api/v1/conversations": {
        items: [
          {
            our_e164: "+12145550111",
            contact_e164: "+19725550250",
            inbox_id: "i2",
            thread_id: "t2",
            contact: { id: "c2", display_name: "Bob Viewer" },
            snippet: "hi there",
            last_event_type: "message",
            direction: "inbound",
            last_event_at: new Date().toISOString(),
            unread: 0,
            status: "open",
          },
        ],
        next_cursor: null,
      },
    });
    renderPageAt("/inbox?inbox=all", client);

    await userEvent.click(await screen.findByRole("button", { name: /Bob Viewer/ }));

    expect(
      await screen.findByText("Read-only inbox — you can view but not send"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeDisabled();
  });
});
