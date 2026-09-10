import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { CommandPalette } from "@/components/ui/CommandPalette";
import { Sidebar } from "@/components/shell/Sidebar";
import type { ContactOut } from "@/api/contacts";
import type { Conversation, CursorPage, Inbox } from "@/api/conversations";

const ME: Me = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "owner",
      permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
    },
  ],
};

const FULL_CAPS = {
  permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
  org: {
    has_provider: false,
    has_number: false,
    member_count: 1,
    registration_state: "none",
  },
};

const inboxes: Inbox[] = [
  {
    id: "i1",
    name: "Sales",
    color: "#22c55e",
    e164: "+14694617576",
    number_id: "n1",
    my_role: "admin",
  },
  {
    id: "i2",
    name: "Support",
    color: "#3b82f6",
    e164: "+12145550111",
    number_id: "n2",
    my_role: "admin",
  },
];

const contacts: ContactOut[] = [
  {
    id: "c1",
    display_name: "Alice",
    phones: [{ e164: "+14155550101", is_primary: true }],
    owner_user_id: null,
    department_id: null,
  },
  {
    id: "c2",
    display_name: "Bob",
    phones: [{ e164: "+14155550102", is_primary: true }],
    owner_user_id: null,
    department_id: null,
  },
];

const conversations: Conversation[] = [
  {
    our_e164: "+14694617576",
    contact_e164: "+14155550101",
    inbox_id: "i1",
    thread_id: "t1",
    contact: { id: "c1", display_name: "Alice" },
    snippet: "Hi",
    last_event_type: "message",
    direction: "inbound",
    last_event_at: new Date().toISOString(),
    unread: false,
    status: "open",
  },
  {
    our_e164: "+14694617576",
    contact_e164: "+14155550102",
    inbox_id: "i2",
    thread_id: null,
    contact: { id: "c2", display_name: null },
    snippet: null,
    last_event_type: "call",
    direction: null,
    last_event_at: new Date().toISOString(),
    unread: false,
    status: "open",
  },
];

function paletteRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": FULL_CAPS,
    "/api/v1/inboxes": inboxes,
    "/api/v1/contacts": contacts,
    "/api/v1/conversations": { items: conversations, next_cursor: null } as CursorPage<Conversation>,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderPalette(routes: Record<string, unknown> = paletteRoutes(), ui?: ReactNode) {
  const client = makeStubClient(routes);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={["/inbox"]}>
          {ui}
          <CommandPalette />
          <Routes>
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return client;
}

describe("CommandPalette", () => {
  it("Ctrl+K opens the dialog and focuses the search input", async () => {
    renderPalette();

    fireEvent.keyDown(document, { key: "k", ctrlKey: true });

    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Search" })).toHaveFocus();
    });
  });

  it("Meta+K also opens it", async () => {
    renderPalette();

    fireEvent.keyDown(document, { key: "k", metaKey: true });

    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it('clicking the Sidebar "Search" rail button opens it', async () => {
    renderPalette(paletteRoutes(), <Sidebar />);

    const searchButton = await screen.findByRole("button", { name: "Search" });
    await userEvent.click(searchButton);

    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("does not fetch contacts for a one-character query", async () => {
    const client = renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "a" } });
    // Wrapped in act(): the 200 ms debounce fires a setState from a timer, and an
    // unwrapped sleep would let that land outside React's test scope (act warning).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });

    expect(
      client.calls.every((call) => !call.path.startsWith("/api/v1/contacts?q=")),
    ).toBe(true);
  });

  it("debounces typing and requests only the final query", async () => {
    const client = renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.change(input, { target: { value: "al" } });
    fireEvent.change(input, { target: { value: "ali" } });
    fireEvent.change(input, { target: { value: "alic" } });
    fireEvent.change(input, { target: { value: "alice" } });

    await waitFor(() => {
      expect(
        client.calls.filter((call) => call.path.startsWith("/api/v1/contacts?q=")),
      ).toHaveLength(1);
    });

    expect(
      client.calls.find((call) => call.path.startsWith("/api/v1/contacts?q="))?.path,
    ).toContain("q=alice");
  });

  it("renders results as grouped listbox options", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "al" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "group",
        ),
      ).toHaveLength(3);
    });

    const groups = within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
      "group",
    );
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual([
      "Inboxes",
      "Contacts",
      "Conversations",
    ]);
  });

  it("ArrowDown moves active descendant from the first option to the second", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "al" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "option",
        ).length,
      ).toBeGreaterThan(1);
    });

    const options = within(
      screen.getByRole("listbox", { name: "Search results" }),
    ).getAllByRole("option");
    expect(input).toHaveAttribute("aria-activedescendant", options[0].id);

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input).toHaveAttribute("aria-activedescendant", options[1].id);
  });

  it("ArrowUp from the first option wraps to the last", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "al" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "option",
        ).length,
      ).toBeGreaterThan(0);
    });

    const options = within(
      screen.getByRole("listbox", { name: "Search results" }),
    ).getAllByRole("option");

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(input).toHaveAttribute(
      "aria-activedescendant",
      options[options.length - 1].id,
    );
  });

  it("Enter on a contact navigates to /contacts/<id> and closes", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "alice" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "option",
        ).length,
      ).toBeGreaterThan(0);
    });

    const firstOption = within(
      screen.getByRole("listbox", { name: "Search results" }),
    ).getAllByRole("option")[0];
    expect(firstOption.id).toContain("contact-c1");

    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/contacts/c1");
    });
    expect(screen.queryByRole("dialog", { name: "Search" })).not.toBeInTheDocument();
  });

  it("Enter on an inbox navigates to /inbox/<inboxId>", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "sales" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "option",
        ).length,
      ).toBeGreaterThan(0);
    });

    const firstOption = within(
      screen.getByRole("listbox", { name: "Search results" }),
    ).getAllByRole("option")[0];
    expect(firstOption.id).toContain("inbox-i1");

    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/inbox/i1");
    });
  });

  it("Enter on a conversation navigates to /inbox/<inboxId>?contact=...&our=...", async () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "alice" } });

    await waitFor(() => {
      expect(
        within(screen.getByRole("listbox", { name: "Search results" })).getAllByRole(
          "option",
        ).length,
      ).toBeGreaterThan(2);
    });

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      const location = screen.getByTestId("location").textContent ?? "";
      expect(location.startsWith("/inbox/i1")).toBe(true);
      expect(location).toContain("contact=%2B14155550101");
      expect(location).toContain("our=%2B14694617576");
    });
  });

  it("Escape closes the dialog and returns focus to the rail Search button", async () => {
    renderPalette(paletteRoutes(), <Sidebar />);

    const searchButton = await screen.findByRole("button", { name: "Search" });
    await userEvent.click(searchButton);

    const input = screen.getByRole("combobox", { name: "Search" });
    await waitFor(() => expect(input).toHaveFocus());

    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Search" })).not.toBeInTheDocument();
    });
    expect(searchButton).toHaveFocus();
  });

  it('renders "No matches" empty state for a query with no results', async () => {
    renderPalette(
      paletteRoutes({
        "/api/v1/contacts": [] as ContactOut[],
        "/api/v1/conversations": {
          items: [],
          next_cursor: null,
        } as CursorPage<Conversation>,
      }),
    );

    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByRole("combobox", { name: "Search" });

    fireEvent.change(input, { target: { value: "zzzz" } });

    await waitFor(() => {
      expect(screen.getByText("No matches")).toBeInTheDocument();
    });
  });
});
