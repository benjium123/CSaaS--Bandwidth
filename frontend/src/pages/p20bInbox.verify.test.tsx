/**
 * P20b VERIFICATION probes (Opus verifier, 2026-09-10).
 *
 * These are verifier-authored tests. They exist to prove the P20b inbox surface actually
 * behaves the way the plan addendum and the supervisor's VERDICT.md claim, over the real
 * `<Routes>` shape from App.tsx (which the existing ConversationsPage.test.tsx never
 * mounts - it renders the page bare, so `useParams()` is always `{}` there).
 *
 * Covered: route/alias inbox selection, row-click -> URL, the Important/Unresponded
 * FILTER CHIPS (they moved out of the inbox column into the conversation list when the
 * console reference removed the duplicated Views block), "+ New", the star-toggle
 * regression (F1) with InboxColumn mounted, the header call button's dial arguments,
 * the contact panel's P22 owner/team + "Open contact" link + Collapsible storageKey
 * persistence, the panel's hidden-until-asked-for default, and below-sm mobile.
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
  // The inbox rail now mounts NotificationBell (the reference's Notifications item), and
  // the bell reads the realtime socket through this hook. Returning null IS the "no
  // socket" case the hook exists for - the bell falls back to polling.
  useOptionalSoftphone: () => null,
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

function conversationList() {
  return within(screen.getByRole("complementary", { name: "Conversation list" }));
}

/** ConversationHeader's root is a <header> nested inside <main>/<section>, so it does NOT
 * expose the `banner` role - query the element itself. It is the only <header> this page
 * renders, and it only exists once a conversation is selected. */
async function threadHeader() {
  // The subtitle used to read "· via <our number>" and is now the reference's
  // "<their number> · <the line's name>", where the line's name ("Sales") also appears in
  // the inbox column - so there is no longer a string unique to the header to wait on.
  // Wait for the header ELEMENT instead, which is what this helper was always after.
  await waitFor(() => {
    if (!document.querySelector("header")) throw new Error("thread header is not rendered");
  });
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

  // MOVED, not dropped: the same filter=important round trip, driven from the chip in
  // the conversation list. The inbox column no longer offers it - the row and the chip
  // were two controls over one `filter` value, which is exactly what the console
  // reference removed.
  it("the Important chip applies filter=important and toggles back off", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    const important = await conversationList().findByRole("button", { name: "Important" });
    await userEvent.click(important);

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("filter=important"))).toBe(true),
    );
    await waitFor(() => expect(important).toHaveAttribute("aria-pressed", "true"));

    // Pressing the active pill again returns to "open".
    await userEvent.click(important);
    await waitFor(() => expect(listRequests(client).at(-1)).toContain("filter=open"));
  });

  it("the Unresponded chip applies filter=unresponded", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);

    await userEvent.click(
      await conversationList().findByRole("button", { name: "Unresponded" }),
    );

    await waitFor(() =>
      expect(listRequests(client).some((p) => p.includes("filter=unresponded"))).toBe(true),
    );
  });

  it("the inbox column no longer carries any filter row", async () => {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);
    await screen.findByText("Ada Lovelace");

    for (const label of ["Important", "Unresponded", "Snoozed", "Overdue"]) {
      expect(inboxColumn().queryByRole("button", { name: label })).toBeNull();
    }
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
  /** The panel is hidden until asked for (console-reference.html), so opening it is now
   * two steps: pick the conversation, then press the panel toggle. */
  async function openPanel() {
    const client = makeStubClient(routes());
    renderAt("/inbox/i1", client);
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Toggle contact panel" }),
    );
    return within(await screen.findByRole("complementary", { name: "Contact panel" }));
  }

  it("stays shut when a conversation is selected, and opens only on the toggle", async () => {
    const client = makeStubClient(routes());
    const { container } = renderAt("/inbox/i1", client);

    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));

    // The grid column is collapsed to 0px and the toggle reads "not pressed".
    const app = container.querySelector("[data-panel]") as HTMLElement;
    expect(app.dataset.panel).toBe("closed");
    const toggle = await screen.findByRole("button", { name: "Toggle contact panel" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(toggle);

    expect(app.dataset.panel).toBe("open");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
  });

  // Owner/Team no longer render here at all: they moved to ContactsPage's table columns
  // and AssignOwnerDrawer, which is the only place either is EDITABLE (verified: both
  // columns at ContactsPage.tsx:401-402, the drawer mounted at :498). The absence half is
  // paired with a presence half on purpose - an absence-only assertion would also pass if
  // the panel rendered nothing at all, which is the failure it is meant to catch.
  it("shows the four reference fields at rest, and no longer Owner or Team", async () => {
    const panel = await openPanel();

    expect(await panel.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(panel.getByText("Phone")).toBeInTheDocument();
    expect(panel.getByText("Email")).toBeInTheDocument();
    expect(panel.getByText("Company")).toBeInTheDocument();

    expect(panel.queryByText("Grace Hopper")).toBeNull();
    expect(panel.queryByText("Support Team")).toBeNull();
  });

  // The route through to the full record survives, but the AVATAR carries it now rather
  // than a fifth row captioned "Open contact", so the head stays two things.
  it("links the avatar to /contacts/<contactId>", async () => {
    const panel = await openPanel();
    expect(
      await panel.findByRole("link", { name: "Open contact record for Ada Lovelace" }),
    ).toHaveAttribute("href", "/contacts/c1");
  });

  // Both disclosures are now `defaultOpen={false}`: at rest the panel IS the four things,
  // and Role/Address/Tags/Notes are one click away rather than deleted. Expanding is
  // asserted to actually reveal Role - "aria-expanded flipped" alone would still pass if
  // the disclosure body were empty.
  it("keeps Details/Notes shut at rest, opens them, and persists that under its storageKey", async () => {
    const panel = await openPanel();
    const details = await panel.findByRole("button", { name: "Details" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    expect(panel.queryByText("Role")).toBeNull();
    expect(localStorage.getItem("contact-panel.details")).toBeNull();

    await userEvent.click(details);
    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(panel.getByText("Role")).toBeInTheDocument();
    expect(localStorage.getItem("contact-panel.details")).toBe("true");

    await userEvent.click(panel.getByRole("button", { name: "Notes" }));
    expect(localStorage.getItem("contact-panel.notes")).toBe("true");
  });

  // Direction flipped deliberately. Once `defaultOpen` became false, seeding "false" here
  // agreed with the default, so this passed whether or not localStorage was read at all -
  // it survived the change by becoming vacuous rather than by staying true. Seeding "true"
  // is the only version that can fail if the read path breaks.
  it("reads a pre-existing expanded state back out of localStorage on mount", async () => {
    localStorage.setItem("contact-panel.details", "true");
    const panel = await openPanel();
    expect(await panel.findByRole("button", { name: "Details" })).toHaveAttribute(
      "aria-expanded",
      "true",
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
