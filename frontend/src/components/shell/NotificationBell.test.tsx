import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { NotificationBell } from "./NotificationBell";
import type { ApiClient } from "@/api/client";
import type { Notification, NotificationsPage } from "@/api/inboxPro";

type RouteHandler = (path: string, init: RequestInit & { json?: unknown }) => unknown;

function makeNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "n1",
    kind: "mention",
    thread_id: null,
    our_e164: null,
    contact_e164: null,
    body: "Mentioned you",
    read_at: null,
    created_at: "2024-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function pageOf(items: Notification[], unread_count: number): NotificationsPage {
  return { items, unread_count };
}

let lastLocation = "";

function LocationProbe() {
  const location = useLocation();
  lastLocation = `${location.pathname}${location.search}`;
  return null;
}

/** Renders with a real MemoryRouter (not renderWithProviders' bare one) plus a probe, so
 * navigation triggered by clicking a notification can be asserted. */
function renderBellWithLocation(client: ApiClient) {
  lastLocation = "";
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={["/somewhere"]}>
          <NotificationBell />
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function makeBellClient(
  page: NotificationsPage,
  markRead: RouteHandler = (_path: string, _init: RequestInit & { json?: unknown }) => ({
    updated: 1,
  }),
) {
  return makeStubClient({
    "/api/v1/auth/me": {
      id: "u1",
      email: "a@example.com",
      full_name: "A",
      memberships: [],
    },
    "/api/v1/me/notifications/read": markRead,
    "/api/v1/me/notifications": (_path: string, _init: RequestInit & { json?: unknown }) => page,
  });
}

describe("NotificationBell", () => {
  it("renders a bell with no badge and the name Alerts when nothing is unread", async () => {
    const client = makeBellClient(pageOf([], 0));
    renderWithProviders(<NotificationBell />, client);

    const trigger = await screen.findByRole("button", { name: "Alerts" });
    expect(trigger).toBeInTheDocument();
    expect(within(trigger).queryByText("0")).not.toBeInTheDocument();
  });

  it("names itself Alerts, 3 unread and shows a 3 badge when three are unread", async () => {
    const client = makeBellClient(
      pageOf(
        [
          makeNotification({ id: "n1" }),
          makeNotification({ id: "n2" }),
          makeNotification({ id: "n3" }),
        ],
        3,
      ),
    );
    renderWithProviders(<NotificationBell />, client);

    const trigger = await screen.findByRole("button", { name: "Alerts, 3 unread" });
    expect(within(trigger).getByText("3")).toBeInTheDocument();
  });

  it("shows 9+ for a count above nine", async () => {
    const client = makeBellClient(pageOf([makeNotification({ id: "n1" })], 12));
    renderWithProviders(<NotificationBell />, client);

    const trigger = await screen.findByRole("button", { name: "Alerts, 12 unread" });
    expect(within(trigger).getByText("9+")).toBeInTheDocument();
  });

  it("renders the bell and does not throw when there is no stub for the path", async () => {
    const client = makeStubClient({});
    renderWithProviders(<NotificationBell />, client);

    expect(await screen.findByRole("button", { name: "Alerts" })).toBeInTheDocument();
  });

  it("opens the menu with groups in the order Mentions, Assigned to you, Overdue, Missed calls", async () => {
    const client = makeBellClient(
      pageOf(
        [
          makeNotification({
            id: "overdue1",
            kind: "overdue",
            body: "Overdue item",
            created_at: "2024-01-04T00:00:00.000Z",
          }),
          makeNotification({
            id: "mention1",
            kind: "mention",
            body: "Mentioned you",
            created_at: "2024-01-01T00:00:00.000Z",
          }),
          makeNotification({
            id: "assignment1",
            kind: "assignment",
            body: "Assigned item",
            created_at: "2024-01-02T00:00:00.000Z",
          }),
          makeNotification({
            id: "missed1",
            kind: "missed_call",
            body: "Missed call item",
            created_at: "2024-01-03T00:00:00.000Z",
          }),
        ],
        4,
      ),
    );
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 4 unread" }));
    const menu = await screen.findByRole("menu", { name: "Alerts" });
    const text = menu.textContent ?? "";

    expect(text.indexOf("Mentions")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("Mentions")).toBeLessThan(text.indexOf("Assigned to you"));
    expect(text.indexOf("Assigned to you")).toBeLessThan(text.indexOf("Overdue"));
    expect(text.indexOf("Overdue")).toBeLessThan(text.indexOf("Missed calls"));
  });

  it("marks unread items with the Unread screen-reader marker and read ones without", async () => {
    const client = makeBellClient(
      pageOf(
        [
          makeNotification({ id: "n1", body: "Unread mention", read_at: null }),
          makeNotification({
            id: "n2",
            kind: "assignment",
            body: "Read assignment",
            read_at: "2024-01-02T00:00:00.000Z",
          }),
        ],
        1,
      ),
    );
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    const menu = await screen.findByRole("menu", { name: "Alerts" });

    const unreadItem = within(menu).getByRole("menuitem", { name: /Unread mention/ });
    expect(within(unreadItem).getByText("Unread")).toBeInTheDocument();

    const readItem = within(menu).getByRole("menuitem", { name: /Read assignment/ });
    expect(within(readItem).queryByText("Unread")).not.toBeInTheDocument();
  });

  it("clicking an item POSTs {ids:[id]} to /api/v1/me/notifications/read", async () => {
    const client = makeBellClient(
      pageOf([makeNotification({ id: "n1", body: "Mentioned you" })], 1),
    );
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    const menu = await screen.findByRole("menu", { name: "Alerts" });
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Mentioned you/ }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/me/notifications/read" && call.init.method === "POST",
      );
      expect(post).toBeTruthy();
      expect(post?.init.json).toEqual({ ids: ["n1"] });
    });
  });

  it("Mark all as read POSTs {all: true}", async () => {
    const client = makeBellClient(pageOf([makeNotification({ id: "n1" })], 1));
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    await userEvent.click(screen.getByRole("button", { name: "Mark all as read" }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/me/notifications/read" && call.init.method === "POST",
      );
      expect(post).toBeTruthy();
      expect(post?.init.json).toEqual({ all: true });
    });
  });

  it("does not show Mark all as read when nothing is unread", async () => {
    const client = makeBellClient(
      pageOf([makeNotification({ id: "n1", read_at: "2024-01-01T00:00:00.000Z" })], 0),
    );
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts" }));
    expect(screen.queryByRole("button", { name: "Mark all as read" })).not.toBeInTheDocument();
  });

  it("shows the empty state Nothing new.", async () => {
    const client = makeBellClient(pageOf([], 0));
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts" }));
    expect(screen.getByText("Nothing new.")).toBeInTheDocument();
  });

  it("closes on Escape and on an outside mousedown", async () => {
    const client = makeBellClient(pageOf([makeNotification({ id: "n1" })], 1));
    renderWithProviders(<NotificationBell />, client);

    const trigger = await screen.findByRole("button", { name: "Alerts, 1 unread" });

    await userEvent.click(trigger);
    expect(screen.getByRole("menu", { name: "Alerts" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu", { name: "Alerts" })).not.toBeInTheDocument();

    await userEvent.click(trigger);
    expect(screen.getByRole("menu", { name: "Alerts" })).toBeInTheDocument();

    await userEvent.click(document.body);
    expect(screen.queryByRole("menu", { name: "Alerts" })).not.toBeInTheDocument();
  });

  it("clicking a notification with our_e164/contact_e164 navigates to it and closes the menu", async () => {
    const client = makeBellClient(
      pageOf(
        [
          makeNotification({
            id: "n1",
            body: "Mentioned you",
            thread_id: "t1",
            our_e164: "+15005550006",
            contact_e164: "+15005550001",
          }),
        ],
        1,
      ),
    );
    renderBellWithLocation(client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    const menu = await screen.findByRole("menu", { name: "Alerts" });
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Mentioned you/ }));

    await waitFor(() => expect(lastLocation).toContain("/inbox?"));
    expect(lastLocation).toContain("contact=%2B15005550001");
    expect(lastLocation).toContain("our=%2B15005550006");
    expect(screen.queryByRole("menu", { name: "Alerts" })).not.toBeInTheDocument();
  });

  it("clicking a notification with no thread pair marks it read but does not navigate", async () => {
    const client = makeBellClient(
      pageOf([makeNotification({ id: "n1", body: "Missed call from someone" })], 1),
    );
    renderBellWithLocation(client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    const menu = await screen.findByRole("menu", { name: "Alerts" });
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Missed call/ }));

    expect(lastLocation).toBe("/somewhere");
    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/me/notifications/read" && call.init.method === "POST",
      );
      expect(post).toBeTruthy();
    });
  });

  it("shows a failed mark-read message", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": {
        id: "u1",
        email: "a@example.com",
        full_name: "A",
        memberships: [],
      },
      "/api/v1/me/notifications/read": (_path: string, _init: RequestInit & { json?: unknown }) => {
        throw new Error("mark failed");
      },
      "/api/v1/me/notifications": (_path: string, _init: RequestInit & { json?: unknown }) =>
        pageOf([makeNotification({ id: "n1" })], 1),
    });
    renderWithProviders(<NotificationBell />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Alerts, 1 unread" }));
    await userEvent.click(screen.getByRole("button", { name: "Mark all as read" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("mark failed");
  });
});
