/**
 * P20b VERIFICATION probes (Opus verifier, 2026-09-10).
 *
 * These are verifier-authored tests. They exist to prove the P20b inbox surface actually
 * behaves the way the plan addendum and the supervisor's VERDICT.md claim, over the real
 * `<Routes>` shape from App.tsx (which the existing ConversationsPage.test.tsx never
 * mounts - it renders the page bare, so `useParams()` is always `{}` there).
 *
 * Covered: route/alias inbox selection, row-click -> URL, Important/Unresponded rows,
 * "+ New", the star-toggle regression (F1) with InboxColumn mounted, the header call
 * button's dial arguments, the contact panel's P22 owner/team + "Open contact" link +
 * Collapsible storageKey persistence, and the below-sm mobile behaviour.
 *
 * Do not weaken these into smoke tests.
 */
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ApiClient } from "@/api/client";
import { AuthProvider } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { ConversationsPage } from "./ConversationsPage";
import type { Conversation, Inbox } from "@/api/conversations";

/** The softphone is mocked wholesale so `dial` is directly assertable (item 3) and no
 * livekit/WebSocket fakes are needed. ConversationsPage uses `subscribe`; the header,
 * the contact panel and PhoneNumberMenu use `dial`. */
const { dialMock, subscribeMock } = vi.hoisted(() => ({
  dialMock: vi.fn(),
  subscribeMock: vi.fn(),
}));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => children,
  useSoftphone: () => ({ dial: dialMock, subscribe: subscribeMock }),
}));

beforeEach(() => {
  dialMock.mockReset();
  dialMock.mockResolvedValue(undefined);
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

const SALES: Inbox = {
  id: "i1",
  name: "Sales",
  color: "#22c55e",
  e164: "+14694617576",
  number_id: "n1",
  my_role: "admin",
};

const SUPPORT: Inbox = {
  id: "i2",
  name: "Support",
  color: "#3b82f6",
  e164: "+19729305420",
  number_id: "n2",
  my_role: "admin",
};

const CONTACT_E164 = "+19725550199";
const TIMELINE_PATH = `/api/v1/conversations/${encodeURIComponent(CONTACT_E164)}/timeline`;

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    our_e164: SALES.e164,
    contact_e164: CONTACT_E164,
    inbox_id: SALES.id,
    thread_id: "t1",
    contact: { id: "c1", display_name: "Ada Lovelace" },
    snippet: "hello there",
    last_event_type: "message",
    direction: "inbound",
    last_event_at: new Date().toISOString(),
    unread: true,
    status: "open",
    important: false,
    ...overrides,
  };
}

/** StartsWith-matched stubs: the MORE SPECIFIC path must be listed first. */
function routes(extra: Record<string, unknown> = {}) {
  return {
    [TIMELINE_PATH]: { items: [], next_cursor: null },
    "/api/v1/conversations": { items: [conversation()], next_cursor: null },
    "/api/v1/inboxes/i1/grants": [],
    "/api/v1/inboxes/i2/grants": [],
    "/api/v1/inboxes": [SALES, SUPPORT],
    "/api/v1/orgs/current/members": [
      { user_id: "u2", full_name: "Grace Hopper", email: "g@example.com", role_name: "member" },
    ],
    "/api/v1/departments": [
      { id: "d1", name: "Support Team", is_active: true, member_user_ids: ["u2"] },
    ],
    "/api/v1/contacts/c1/notes": [],
    "/api/v1/contacts/c1": {
      id: "c1",
      display_name: "Ada Lovelace",
      attributes: {},
      phones: [],
      owner_user_id: "u2",
      department_id: "d1",
    },
    "/api/v1/inbox/important-pair": {},
    ...extra,
  };
}

let lastLocation = "";

function LocationProbe() {
  const location = useLocation();
  lastLocation = `${location.pathname}${location.search}`;
  return <div data-testid="location">{lastLocation}</div>;
}

/** The REAL route shape from App.tsx, so `useParams()` actually yields :inboxId. */
function renderAt(path: string, client: ApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/inbox" element={<ConversationsPage />} />
            <Route path="/inbox/:inboxId" element={<ConversationsPage />} />
            <Route path="/inbox/:inboxId/:threadId" element={<ConversationsPage />} />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

/** Every /api/v1/conversations LIST request (i.e. not the unread tally, not the
 * timeline), most recent last. */
function listRequests(client: ReturnType<typeof makeStubClient>): string[] {
  return client.calls
    .map((call) => call.path)
    .filter(
      (path) =>
        path.startsWith("/api/v1/conversations") &&
        !path.includes("/timeline") &&
        !path.includes("filter=unread"),
    );
}

function inboxColumn() {
  return within(screen.getByRole("complementary", { name: "Inbox column" }));
}

/** ConversationHeader's root is a <header> nested inside <main>/<section>, so it does NOT
 * expose the `banner` role - query the element itself. It is the only <header> this page
 * renders, and it only exists once a conversation is selected. */
async function threadHeader() {
  await screen.findByText("· via");
  const el = document.querySelector("header");
  if (!el) throw new Error("thread header is not rendered");
  return within(el as HTMLElement);
}

describe("P20b routing", () => {
  it("/inbox with no param falls back to the first inbox and seeds ?inbox=", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox", client);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("inbox_id=i1"))).toBe(true),
    );
    await waitFor(() => expect(lastLocation).toContain("inbox=i1"));
  });

  it("/inbox/:inboxId selects that inbox, not the first one", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i2", client);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("inbox_id=i2"))).toBe(true),
    );
    // The route param must WIN - the auto-select-first effect must never fire here.
    expect(listRequests(client).some((p) => p.includes("inbox_id=i1"))).toBe(false);
    expect(lastLocation).not.toContain("inbox=");
  });

  it("/inbox/:inboxId/:threadId selects the inbox and ignores :threadId (documented)", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i2/t1", client);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("inbox_id=i2"))).toBe(true),
    );
    expect(listRequests(client).some((p) => p.includes("inbox_id=i1"))).toBe(false);
    // :threadId is deliberately NOT a conversation selector (a call-only pair has a null
    // thread_id) - nothing is selected until ?contact=/?our= say so.
    // "Select a conversation" is rendered twice - the empty header AND the empty timeline.
    expect((await screen.findAllByText("Select a conversation")).length).toBeGreaterThan(0);
  });

  it("the ?inbox= alias still selects an inbox", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox?inbox=i2", client);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("inbox_id=i2"))).toBe(true),
    );
    expect(listRequests(client).some((p) => p.includes("inbox_id=i1"))).toBe(false);
  });

  it("/inbox/all and ?inbox=all send no inbox_id at all", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/all", client);

    await waitFor(() => expect(listRequests(client).length).toBeGreaterThan(0));
    expect(listRequests(client).every((p) => !p.includes("inbox_id="))).toBe(true);
  });

  it("selecting an inbox row navigates to /inbox/<id>", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await inboxColumn().findByRole("button", { name: "Support" }));

    await waitFor(() => expect(lastLocation).toBe("/inbox/i2"));
    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("inbox_id=i2"))).toBe(true),
    );
  });

  it("selecting a conversation row puts ?contact=/?our= on the URL", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    await waitFor(() => expect(lastLocation).toContain("contact=%2B19725550199"));
    expect(lastLocation).toContain("our=%2B14694617576");
    expect(lastLocation.startsWith("/inbox/i1")).toBe(true);
  });

  it("the Important row applies filter=important and toggles back off", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    const important = await inboxColumn().findByRole("button", { name: "Important" });
    await userEvent.click(important);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("filter=important"))).toBe(true),
    );
    await waitFor(() => expect(important).toHaveAttribute("aria-current", "true"));

    // Clicking the selected row again returns to "open", matching the list's chips.
    await userEvent.click(important);
    await waitFor(() =>
      expect(listRequests(client).at(-1)).toContain("filter=open"),
    );
  });

  it("the Unresponded row applies filter=unresponded", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await inboxColumn().findByRole("button", { name: "Unresponded" }));

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("filter=unresponded"))).toBe(true),
    );
  });

  it('the inbox column\'s "+ New" opens NewConversationPanel', async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(
      await inboxColumn().findByRole("button", { name: "New conversation" }),
    );
    await userEvent.click(
      within(inboxColumn().getByRole("menu", { name: "New conversation" })).getByRole(
        "menuitem",
        { name: "New text message" },
      ),
    );

    expect(await screen.findByLabelText("To")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });
});

describe("P20b star-toggle regression (VERDICT F1)", () => {
  it("stars a conversation with InboxColumn mounted: no throw, optimistic flip, POST fires", async () => {
    // The unread tally query must live OUTSIDE the ["conversations"] key family. When it
    // did not, ConversationHeader's setQueriesData({queryKey:["conversations"]}) hit it,
    // threw on `data.pages`, and killed the mutation before mutationFn ever ran.
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // Never resolves: holds the optimistic state still so it is actually observable.
    const hang = new Promise(() => undefined);
    const client = makeStubClient(routes({ "/api/v1/inbox/important-pair": () => hang }));
    renderAt("/inbox/i1", client);

    // The InboxColumn is mounted AND its unread tally has resolved - i.e. the query that
    // used to poison the cache is live at the moment we star.
    expect(await inboxColumn().findByLabelText("1 unread")).toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    const header = await threadHeader();
    await userEvent.click(header.getByRole("button", { name: "Mark as important" }));

    // mutationFn actually ran.
    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/inbox/important-pair" && call.init.method === "POST",
        ),
      ).toBe(true),
    );
    const post = client.calls.find((call) => call.path === "/api/v1/inbox/important-pair");
    expect(post?.init.json).toEqual({
      our_e164: SALES.e164,
      contact_e164: CONTACT_E164,
      important: true,
    });

    // Optimistic flip visible in the header while the request is still in flight.
    expect(await header.findByRole("button", { name: "Unmark as important" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // No error surfaced anywhere, and the unread badge survived the cache write.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(inboxColumn().getByLabelText("1 unread")).toBeInTheDocument();
    expect(
      errorSpy.mock.calls.filter((args) => String(args[0]).includes("pages")),
    ).toHaveLength(0);
  });
});

describe("P20b thread header call button", () => {
  it("dials the contact FROM the selected inbox's number", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    const header = await threadHeader();
    await userEvent.click(header.getByRole("button", { name: "Call Ada Lovelace" }));

    expect(dialMock).toHaveBeenCalledTimes(1);
    expect(dialMock).toHaveBeenCalledWith(CONTACT_E164, SALES.e164);
  });

  it("the subtitle PhoneNumberMenu dials from the same number", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    const header = await threadHeader();
    await userEvent.click(
      header.getByRole("button", { name: `Actions for (972) 555-0199` }),
    );
    await userEvent.click(
      within(header.getByRole("menu", { name: "Phone number actions" })).getByRole(
        "menuitem",
        { name: "Call" },
      ),
    );

    expect(dialMock).toHaveBeenCalledWith(CONTACT_E164, SALES.e164);
  });
});

describe("P20b contact panel", () => {
  async function openPanel() {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    return within(await screen.findByRole("complementary", { name: "Contact panel" }));
  }

  it("resolves Owner and Team from the P22 owner_user_id / department_id fields", async () => {
    const panel = await openPanel();
    expect(await panel.findByText("Grace Hopper")).toBeInTheDocument();
    expect(panel.getByText("Support Team")).toBeInTheDocument();
  });

  it('links "Open contact" to /contacts/<contactId>', async () => {
    const panel = await openPanel();
    expect(await panel.findByRole("link", { name: "Open contact" })).toHaveAttribute(
      "href",
      "/contacts/c1",
    );
  });

  it("persists the Details/Notes Collapsible state under its storageKey", async () => {
    const panel = await openPanel();
    const details = await panel.findByRole("button", { name: "Details" });
    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(localStorage.getItem("contact-panel.details")).toBeNull();

    await userEvent.click(details);
    expect(details).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem("contact-panel.details")).toBe("false");

    await userEvent.click(panel.getByRole("button", { name: "Notes" }));
    expect(localStorage.getItem("contact-panel.notes")).toBe("false");
  });

  it("reads a pre-existing collapsed state back out of localStorage on mount", async () => {
    localStorage.setItem("contact-panel.details", "false");
    const panel = await openPanel();
    expect(await panel.findByRole("button", { name: "Details" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

describe("P20b mobile (390px)", () => {
  /** jsdom has no matchMedia at all, so every existing test exercises the desktop path.
   * This stub reports a 390px-wide viewport for the page's "(max-width: 639px)" query. */
  function stubViewport(width: number) {
    vi.stubGlobal(
      "matchMedia",
      (query: string) => {
        const max = /max-width:\s*(\d+)px/.exec(query);
        const matches = max ? width <= Number(max[1]) : false;
        return {
          matches,
          media: query,
          onchange: null,
          addEventListener: () => undefined,
          removeEventListener: () => undefined,
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => false,
        };
      },
    );
  }

  beforeEach(() => stubViewport(390));

  it("renders the inbox column inside a Sheet behind a 'Choose inbox' trigger", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    const trigger = await screen.findByRole("button", { name: "Choose inbox" });
    // Not inline: the column only exists once the sheet is open.
    expect(screen.queryByRole("complementary", { name: "Inbox column" })).not.toBeInTheDocument();

    await userEvent.click(trigger);
    const sheet = await screen.findByRole("dialog", { name: "Inboxes" });
    expect(sheet).toHaveAttribute("aria-modal", "true");
    expect(within(sheet).getByRole("complementary", { name: "Inbox column" })).toBeInTheDocument();
  });

  it("does NOT auto-open the contact sheet when a conversation row is clicked (VERDICT F8)", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await threadHeader();

    // Below sm the contact panel is a modal, focus-trapping Sheet - auto-opening it would
    // trap the user the instant they pick a conversation.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the contact panel as a Sheet from the panel toggle", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Toggle contact panel" }));

    const sheet = await screen.findByRole("dialog", { name: "Contact" });
    expect(within(sheet).getByRole("complementary", { name: "Contact panel" })).toBeInTheDocument();
  });

  it("toggles list <-> thread: Back clears the selection", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await waitFor(() => expect(lastLocation).toContain("contact=%2B19725550199"));

    await userEvent.click(
      await screen.findByRole("button", { name: "Back to conversation list" }),
    );
    await waitFor(() => expect(lastLocation).not.toContain("contact="));
    expect(lastLocation).not.toContain("our=");
    // "Select a conversation" is rendered twice - the empty header AND the empty timeline.
    expect((await screen.findAllByText("Select a conversation")).length).toBeGreaterThan(0);
  });
});
