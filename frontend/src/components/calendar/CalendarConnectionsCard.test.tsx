import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CalendarConnectionsCard } from "./CalendarConnectionsCard";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { AuthProvider } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";

function makeConnection(overrides: Record<string, unknown> = {}) {
  return {
    id: "c1",
    provider: "google",
    account_email: "owner@example.com",
    status: "active",
    last_error: null,
    connected_by_me: true,
    created_at: "2026-10-07T00:00:00+00:00",
    ...overrides,
  };
}

/**
 * renderWithProviders always mounts a bare MemoryRouter; the banner test needs an initial
 * URL, so rebuild the exact same provider stack with initialEntries (as in test/harness.tsx).
 */
function renderAt(ui: React.ReactNode, client: ApiClient, entries: string[]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={entries}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

let restoreLocation: (() => void) | null = null;

afterEach(() => {
  restoreLocation?.();
  restoreLocation = null;
});

/** Swap window.location for one whose assign() is a spy, so the OAuth redirect is observable. */
function stubLocationAssign() {
  const original = window.location;
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    value: { ...original, assign },
    writable: true,
    configurable: true,
  });
  restoreLocation = () => {
    Object.defineProperty(window, "location", {
      value: original,
      writable: true,
      configurable: true,
    });
  };
  return assign;
}

describe("CalendarConnectionsCard", () => {
  it("lists a connected Google account and offers the connect button", async () => {
    const client = makeStubClient({
      "/api/v1/calendar/connections": {
        google_available: true,
        connections: [makeConnection()],
      },
    });
    renderWithProviders(<CalendarConnectionsCard />, client);

    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Google Calendar" })).toBeInTheDocument();
  });

  it("hides the connect button when Google is not available", async () => {
    const client = makeStubClient({
      "/api/v1/calendar/connections": { google_available: false, connections: [] },
    });
    renderWithProviders(<CalendarConnectionsCard />, client);

    expect(
      await screen.findByText("Google Calendar connection is not available yet."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect Google Calendar" }),
    ).not.toBeInTheDocument();
  });

  it("sends the browser to Google's consent URL when connecting", async () => {
    const assign = stubLocationAssign();
    const client = makeStubClient({
      "/api/v1/calendar/oauth/google/start": { url: "https://accounts.google.com/x" },
      "/api/v1/calendar/connections": { google_available: true, connections: [] },
    });
    renderWithProviders(<CalendarConnectionsCard />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Connect Google Calendar" }),
    );

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://accounts.google.com/x"),
    );
  });

  it("confirms inline before disconnecting a calendar", async () => {
    const client = makeStubClient({
      "/api/v1/calendar/connections": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => {
        if ((init.method ?? "GET") === "DELETE") return undefined;
        return { google_available: true, connections: [makeConnection()] };
      },
    });
    renderWithProviders(<CalendarConnectionsCard />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Disconnect owner@example.com" }),
    );
    expect(
      screen.getByText("Remove this calendar? The AI will stop checking it."),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(
      screen.queryByText("Remove this calendar? The AI will stop checking it."),
    ).not.toBeInTheDocument();
    expect(client.calls.some((c) => c.init.method === "DELETE")).toBe(false);

    await userEvent.click(
      screen.getByRole("button", { name: "Disconnect owner@example.com" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) =>
            c.path === "/api/v1/calendar/connections/c1" && c.init.method === "DELETE",
        ),
      ).toBe(true),
    );
  });

  it("flags an errored connection with its last error and a reconnect button", async () => {
    const client = makeStubClient({
      "/api/v1/calendar/connections": {
        google_available: true,
        connections: [makeConnection({ status: "error", last_error: "invalid_grant" })],
      },
    });
    renderWithProviders(<CalendarConnectionsCard />, client);

    expect(await screen.findByText("Reconnect needed")).toBeInTheDocument();
    expect(screen.getByText("invalid_grant")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  });

  it("shows the OAuth return banner and lets the operator dismiss it", async () => {
    const client = makeStubClient({
      "/api/v1/calendar/connections": { google_available: true, connections: [] },
    });
    renderAt(<CalendarConnectionsCard />, client, [
      "/settings/ai?tab=appointments&calendar=error&calendar_error=Denied",
    ]);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Google Calendar was not connected:");
    expect(alert).toHaveTextContent("Denied");

    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
