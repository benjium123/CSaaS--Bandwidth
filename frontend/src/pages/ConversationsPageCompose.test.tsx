import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import type { ApiClient } from "@/api/client";
import type { Inbox } from "@/api/conversations";
import { AuthProvider } from "@/auth/AuthContext";
import { ConversationsPage } from "./ConversationsPage";
import { makeStubClient } from "@/test/harness";

const { dialMock, subscribeMock } = vi.hoisted(() => ({
  dialMock: vi.fn(),
  subscribeMock: vi.fn(() => () => undefined),
}));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: ReactNode }) => children,
  useSoftphone: () => ({ dial: dialMock, subscribe: subscribeMock }),
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

const TIMELINE_PATH = "/api/v1/conversations/%2B19725550199/timeline";

function routes(overrides: { inboxes?: Inbox[] } = {}) {
  return {
    [TIMELINE_PATH]: { items: [], next_cursor: null },
    "/api/v1/inboxes": overrides.inboxes ?? [inbox()],
    "/api/v1/conversations": { items: [], next_cursor: null },
    "/api/v1/contacts/c1/notes": [],
    "/api/v1/contacts/c1": {
      id: "c1",
      display_name: "Ada Lovelace",
      attributes: {},
      phones: [],
    },
    "/api/v1/contacts": [],
  };
}

function renderPageAt(path: string, client: ApiClient, children?: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          {children}
          <ConversationsPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

describe("ConversationsPageCompose", () => {
  it("renders the new-message panel prefilled from ?compose without a contacts search", async () => {
    const client = makeStubClient(routes());
    renderPageAt("/inbox?compose=%2B19725550199", client);

    const toInput = (await screen.findByLabelText("To")) as HTMLInputElement;
    expect(toInput.value).toBe("(972) 555-0199");

    // act(): the panel's 300 ms To-field debounce fires a setState from a timer.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 350));
    });
    const contactsSearchCalls = client.calls.filter(
      (call) =>
        call.path === "/api/v1/contacts" || call.path.startsWith("/api/v1/contacts?"),
    );
    expect(contactsSearchCalls).toHaveLength(0);
  });

  it("strips ?compose and ?from from the URL", async () => {
    const client = makeStubClient(routes());
    renderPageAt(
      "/inbox?compose=%2B19725550199&from=%2B14694617576",
      client,
      <LocationProbe />,
    );

    await waitFor(() => {
      const search = screen.getByTestId("location-search").textContent ?? "";
      expect(search).not.toContain("compose=");
      expect(search).not.toContain("from=");
    });
  });

  it("preselects the From option when ?from is a sendable number", async () => {
    const client = makeStubClient(routes());
    renderPageAt("/inbox?compose=%2B19725550199&from=%2B14694617576", client);

    const fromSelect = (await screen.findByLabelText("From")) as HTMLSelectElement;
    expect(fromSelect.value).toBe("+14694617576");
  });

  it("ignores an ?from that is not among the sendable numbers and falls back to the first sendable option", async () => {
    const client = makeStubClient({
      ...routes(),
      "/api/v1/inboxes": [
        inbox({ id: "i1", name: "Sales", e164: "+14694617576", my_role: "admin" }),
        inbox({
          id: "i2",
          name: "Viewer Only",
          e164: "+12145550111",
          number_id: "n2",
          my_role: "viewer",
        }),
      ],
    });
    renderPageAt("/inbox?compose=%2B19725550199&from=%2B12145550111", client);

    const fromSelect = (await screen.findByLabelText("From")) as HTMLSelectElement;
    await waitFor(() => expect(fromSelect.value).toBe("+14694617576"));
  });
});
