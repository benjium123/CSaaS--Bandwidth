import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ApiClient } from "@/api/client";
import { ConversationsPage } from "./ConversationsPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { AuthProvider, useAuth } from "@/auth/AuthContext";
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

/** Same fake events websocket as SoftphoneProvider.test.tsx - lets a test hand-fire a
 * server event (message.received, etc.) without a real socket. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(_data: string) {
    /* no-op */
  }
  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.();
  }
}

function latestWs(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1);
  if (!ws) throw new Error("no websocket was created");
  return ws;
}

beforeEach(() => {
  FakeWebSocket.instances.length = 0;
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
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
    // The more specific "/notes" route must be listed (and therefore matched) before
    // the general "/api/v1/contacts/c1" one below - the stub matcher is startsWith-based.
    "/api/v1/contacts/c1/notes": [],
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

  // Item 2
  it("invalidates the conversations list when a message.received event arrives over the realtime socket", async () => {
    const client = makeStubClient({ ...routes(), "/api/v1/auth/me": ME });
    renderPage(client);

    await screen.findByText("Ada Lovelace");
    const listCallsBefore = client.calls.filter(
      (c) => c.path.startsWith("/api/v1/conversations") && !c.path.includes("timeline"),
    ).length;

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "message.received",
          thread_id: "t1",
          our_e164: "+14694617576",
          contact_e164: "+19725550199",
          message_id: "m9",
        }),
      });
    });

    await waitFor(() => {
      const listCallsAfter = client.calls.filter(
        (c) => c.path.startsWith("/api/v1/conversations") && !c.path.includes("timeline"),
      ).length;
      expect(listCallsAfter).toBeGreaterThan(listCallsBefore);
    });
  });

  // Item 2 (Timeline half)
  it("refetches the OPEN thread's timeline on a matching message.received event, and ignores one for a different pair", async () => {
    const client = makeStubClient({ ...routes(), "/api/v1/auth/me": ME });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await screen.findByText("hello");

    const timelineCallsBefore = () =>
      client.calls.filter((c) => c.path.startsWith(TIMELINE_PATH)).length;
    const before = timelineCallsBefore();

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();

    // A message.received for a DIFFERENT pair must not refetch this open thread.
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "message.received",
          thread_id: "t-other",
          our_e164: "+14694617576",
          contact_e164: "+19725550999",
          message_id: "m-other",
        }),
      });
    });
    // Give any (incorrect) refetch a chance to have started, then confirm it didn't.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(timelineCallsBefore()).toBe(before);

    // A message.received for THIS pair must refetch it.
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "message.received",
          thread_id: "t1",
          our_e164: "+14694617576",
          contact_e164: "+19725550199",
          message_id: "m1",
        }),
      });
    });

    await waitFor(() => expect(timelineCallsBefore()).toBeGreaterThan(before));
  });

  // Item 9
  it("resets the Composer's draft when switching to a different conversation", async () => {
    const client = makeStubClient({
      "/api/v1/inboxes": [inbox()],
      // The specific timeline routes must be listed (and therefore matched) before the
      // general "/api/v1/conversations" route below - the stub matcher is startsWith-based.
      "/api/v1/conversations/%2B19725550199/timeline": { items: [], next_cursor: null },
      "/api/v1/conversations/%2B19725550200/timeline": { items: [], next_cursor: null },
      "/api/v1/conversations": {
        items: [
          {
            our_e164: "+14694617576",
            contact_e164: "+19725550199",
            inbox_id: "i1",
            thread_id: "t1",
            contact: { id: "c1", display_name: "Ada Lovelace" },
            snippet: "hi",
            last_event_type: "message",
            direction: "inbound",
            last_event_at: new Date().toISOString(),
            unread: false,
            status: "open",
          },
          {
            our_e164: "+14694617576",
            contact_e164: "+19725550200",
            inbox_id: "i1",
            thread_id: "t2",
            contact: { id: "c2", display_name: "Bob Bond" },
            snippet: "hey",
            last_event_type: "message",
            direction: "inbound",
            last_event_at: new Date().toISOString(),
            unread: false,
            status: "open",
          },
        ],
        next_cursor: null,
      },
      "/api/v1/contacts/c1/notes": [],
      "/api/v1/contacts/c1": { id: "c1", display_name: "Ada Lovelace", attributes: {}, phones: [] },
      "/api/v1/contacts/c2/notes": [],
      "/api/v1/contacts/c2": { id: "c2", display_name: "Bob Bond", attributes: {}, phones: [] },
    });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    const composerInput = await screen.findByLabelText("Message");
    await userEvent.type(composerInput, "draft for Ada");
    expect(composerInput).toHaveValue("draft for Ada");

    await userEvent.click(screen.getByRole("button", { name: /Bob Bond/ }));
    const composerInputAfterSwitch = await screen.findByLabelText("Message");
    expect(composerInputAfterSwitch).toHaveValue("");
  });

  // Item 10
  it("invalidates the SENT conversation's timeline, not whatever is selected once the send resolves", async () => {
    let resolveSend: (value: unknown) => void = () => undefined;
    const sendPromise = new Promise((resolve) => {
      resolveSend = resolve;
    });
    const client = makeStubClient({
      "/api/v1/inboxes": [inbox()],
      // The specific timeline routes must be listed (and therefore matched) before the
      // general "/api/v1/conversations" route below - the stub matcher is startsWith-based.
      "/api/v1/conversations/%2B19725550199/timeline": { items: [], next_cursor: null },
      "/api/v1/conversations/%2B19725550200/timeline": { items: [], next_cursor: null },
      "/api/v1/conversations": {
        items: [
          {
            our_e164: "+14694617576",
            contact_e164: "+19725550199",
            inbox_id: "i1",
            thread_id: "t1",
            contact: { id: "c1", display_name: "Ada Lovelace" },
            snippet: "hi",
            last_event_type: "message",
            direction: "inbound",
            last_event_at: new Date().toISOString(),
            unread: false,
            status: "open",
          },
          {
            our_e164: "+14694617576",
            contact_e164: "+19725550200",
            inbox_id: "i1",
            thread_id: "t2",
            contact: { id: "c2", display_name: "Bob Bond" },
            snippet: "hey",
            last_event_type: "message",
            direction: "inbound",
            last_event_at: new Date().toISOString(),
            unread: false,
            status: "open",
          },
        ],
        next_cursor: null,
      },
      "/api/v1/contacts/c1/notes": [],
      "/api/v1/contacts/c1": { id: "c1", display_name: "Ada Lovelace", attributes: {}, phones: [] },
      "/api/v1/contacts/c2/notes": [],
      "/api/v1/contacts/c2": { id: "c2", display_name: "Bob Bond", attributes: {}, phones: [] },
      "/api/v1/messages": () => sendPromise,
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider client={client}>
          <MemoryRouter>
            <SoftphoneProvider>
              <ConversationsPage />
            </SoftphoneProvider>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.type(await screen.findByLabelText("Message"), "hello Ada");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    // Switch conversations BEFORE the send resolves.
    await userEvent.click(await screen.findByRole("button", { name: /Bob Bond/ }));

    resolveSend({});

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["timeline", "+19725550199", "+14694617576"],
      }),
    );
  });

  // Item 34
  it("clears ?contact and ?our from the URL when the org changes", async () => {
    function OrgSwitchProbe() {
      const { selectOrg } = useAuth();
      return (
        <>
          <button type="button" onClick={() => selectOrg("org-2")}>
            SwitchOrg
          </button>
          <ConversationsPage />
        </>
      );
    }

    const client = makeStubClient(routes());
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider client={client}>
          <MemoryRouter initialEntries={["/inbox?contact=%2B19725550199&our=%2B14694617576"]}>
            <SoftphoneProvider>
              <OrgSwitchProbe />
            </SoftphoneProvider>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );

    // ?contact selects the conversation immediately - both the list row and the header
    // show "Ada Lovelace", so this must tolerate multiple matches.
    expect(await screen.findAllByText("Ada Lovelace")).not.toHaveLength(0);
    expect(screen.getByLabelText("Message")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "SwitchOrg" }));

    // The Composer only renders once a conversation is selected via ?contact - once the
    // org switch clears it, the conversation is no longer selected.
    await waitFor(() => expect(screen.queryByLabelText("Message")).toBeNull());
  });

  // Item 2: Important filter chip.
  it("requests filter=important when the Important chip is clicked", async () => {
    const client = makeStubClient(routes());
    renderPage(client);
    await screen.findByText("Ada Lovelace");

    await userEvent.click(screen.getByRole("button", { name: "Important" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path.startsWith("/api/v1/conversations?") && c.path.includes("filter=important"),
      );
      expect(call).toBeDefined();
    });
  });

  // Item 3: star toggle, optimistically reflected in the list row.
  it("marks a conversation as important via the header star and shows it starred in the list", async () => {
    let important = false;
    const client = makeStubClient({
      ...routes(),
      "/api/v1/inbox/important-pair": ((_path, init) => {
        important = (init.json as { important: boolean }).important;
        return undefined;
      }) as RouteStub,
      "/api/v1/conversations": () => ({
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
            unread: false,
            status: "open",
            important,
          },
        ],
        next_cursor: null,
      }),
    });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Mark as important" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/inbox/important-pair" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find((c) => c.path === "/api/v1/inbox/important-pair");
    expect(postCall?.init.json).toEqual({
      our_e164: "+14694617576",
      contact_e164: "+19725550199",
      important: true,
    });
    expect(await screen.findByLabelText("Important")).toBeInTheDocument();
  });

  // Item 1: gating.
  it("disables the New button when every inbox is viewer-role", async () => {
    const client = makeStubClient({
      ...routes(),
      "/api/v1/inboxes": [inbox({ my_role: "viewer" })],
    });
    renderPage(client);
    await screen.findByText("Ada Lovelace");

    expect(screen.getByRole("button", { name: "New" })).toBeDisabled();
  });

  // Item 1: New text message.
  it("New text message sends via POST /api/v1/messages and selects the resulting pair", async () => {
    // Deliberately NOT spread from routes() - a spread's own keys always keep their
    // original insertion order, so a NEW key added alongside it (this pair's specific
    // timeline route) would land after routes()'s general "/api/v1/conversations" key
    // and never win the startsWith match. Listing the specific timeline route first here
    // avoids that trap directly.
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550999/timeline": { items: [], next_cursor: null },
      "/api/v1/inboxes": [inbox()],
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
            unread: false,
            status: "open",
          },
        ],
        next_cursor: null,
      },
      "/api/v1/contacts": [],
      "/api/v1/messages": () => ({ id: "m-999" }),
    });
    renderPage(client);
    await screen.findByText("Ada Lovelace");

    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "New text message" }));
    expect(screen.getByRole("heading", { name: "New text message" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("To"), "9725550999");
    await userEvent.type(screen.getByLabelText("Message"), "hi there");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/messages" && c.init.method === "POST"),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/messages" && c.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({
      to: "+19725550999",
      from: "+14694617576",
      body: "hi there",
      allow_reassign: false,
    });

    // The compose panel is gone once the pair is selected.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "New text message" })).not.toBeInTheDocument(),
    );
  });

  // Item 1: New call.
  it("New call dials via the softphone with the chosen From/To and selects the resulting pair", async () => {
    // Same key-ordering note as the "New text message" test above - the pair's specific
    // timeline route must be listed before the general "/api/v1/conversations" route.
    const client = makeStubClient({
      "/api/v1/conversations/%2B19725550999/timeline": { items: [], next_cursor: null },
      "/api/v1/inboxes": [inbox()],
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
            unread: false,
            status: "open",
          },
        ],
        next_cursor: null,
      },
      "/api/v1/contacts": [],
      "/api/v1/calls": ((_path, init) => {
        if (init.method === "POST") {
          return {
            id: "call-9",
            contact_e164: "+19725550999",
            room: "room-9",
            token: "tok-9",
            url: "wss://lk.example.com",
          };
        }
        throw new Error("unexpected request");
      }) as RouteStub,
    });
    renderPage(client);
    await screen.findByText("Ada Lovelace");

    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "New call" }));
    expect(screen.getByRole("heading", { name: "New call" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("To"), "9725550999");
    await userEvent.click(screen.getByRole("button", { name: "Call" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/calls" && c.init.method === "POST"),
      ).toBe(true),
    );
    const dialCall = client.calls.find(
      (c) => c.path === "/api/v1/calls" && c.init.method === "POST",
    );
    expect(dialCall?.init.json).toEqual({ to: "+19725550999", from: "+14694617576", via: "room" });

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "New call" })).not.toBeInTheDocument(),
    );
  });
});
