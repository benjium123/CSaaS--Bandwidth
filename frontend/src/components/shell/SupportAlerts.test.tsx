import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { makeStubClient } from "@/test/harness";
import { SupportAlerts, diffUnread, type UnreadChat } from "./SupportAlerts";

/**
 * useAuth is mocked rather than fed through AuthProvider: the harness has no way to hand the
 * provider an operator `me` (the /me response shape is not part of this test), and this
 * component is inert without `me.is_platform_operator`. Only `useAuth` is replaced.
 */
const session = vi.hoisted(() => ({
  client: null as unknown,
  me: null as unknown,
}));

vi.mock("@/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({ api: session.client, me: session.me, orgId: "o1" }),
  };
});

function chat(id: string, lastVisitorAt: string | null, kind: UnreadChat["kind"] = "visitor"): UnreadChat {
  return { id, name: id, kind, last_visitor_at: lastVisitorAt };
}

function installBridge() {
  const notify = vi.fn();
  const setBadge = vi.fn();
  window.ringliteDesktop = { version: "t", platform: "win32", notify, setBadge };
  return { notify, setBadge };
}

function renderAlerts(state: { chats: UnreadChat[] }, operator = true) {
  const client = makeStubClient({
    "/api/v1/ops/site/chats/unread": () => ({ count: state.chats.length, chats: state.chats }),
    // Anything else (AuthProvider is not mounted here) gets an empty object rather than a throw.
    "/": {},
  });
  session.client = client;
  session.me = operator
    ? { id: "op-1", is_platform_operator: true, memberships: [] }
    : { id: "u-1", memberships: [] };
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  // Built by hand rather than with renderWithProviders because the second-poll assertions need
  // a handle on the QueryClient to invalidate. Same shape as DesktopAlerts.test.tsx.
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SupportAlerts />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, client, queryClient, state };
}

beforeEach(() => {
  delete window.ringliteDesktop;
  session.client = null;
  session.me = null;
});

afterEach(() => {
  cleanup();
  delete window.ringliteDesktop;
  session.client = null;
  session.me = null;
});

describe("diffUnread", () => {
  it("primes on the first poll without reporting anything fresh", () => {
    const { next, fresh } = diffUnread(null, [chat("a", "2024-01-01T00:00:00.000Z")]);
    expect(fresh).toEqual([]);
    expect(next.get("a")).toBe("2024-01-01T00:00:00.000Z");
  });

  it("reports a chat whose last_visitor_at moved", () => {
    const prev = new Map([["a", "2024-01-01T00:00:00.000Z"]]);
    const { next, fresh } = diffUnread(prev, [chat("a", "2024-01-02T00:00:00.000Z")]);
    expect(fresh.map((c) => c.id)).toEqual(["a"]);
    expect(next.get("a")).toBe("2024-01-02T00:00:00.000Z");
  });

  it("reports a chat id it has never seen", () => {
    const prev = new Map([["a", "2024-01-01T00:00:00.000Z"]]);
    const { fresh } = diffUnread(prev, [
      chat("a", "2024-01-01T00:00:00.000Z"),
      chat("b", "2024-01-02T00:00:00.000Z"),
    ]);
    expect(fresh.map((c) => c.id)).toEqual(["b"]);
  });

  it("reports nothing when nothing moved", () => {
    const prev = new Map([["a", "2024-01-01T00:00:00.000Z"]]);
    expect(diffUnread(prev, [chat("a", "2024-01-01T00:00:00.000Z")]).fresh).toEqual([]);
  });
});

describe("SupportAlerts", () => {
  it("primes the first poll without notifying", async () => {
    const { notify } = installBridge();
    const { queryClient } = renderAlerts({ chats: [chat("chat-a", "2024-01-01T00:00:00.000Z")] });

    await waitFor(() =>
      expect(queryClient.getQueryData(["ops", "site-chats-unread"])).toBeDefined(),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(notify).not.toHaveBeenCalled();
  });

  it("notifies the desktop bridge for a chat that appears after priming", async () => {
    const { notify } = installBridge();
    const { queryClient, state } = renderAlerts({
      chats: [chat("chat-a", "2024-01-01T00:00:00.000Z")],
    });

    await waitFor(() =>
      expect(queryClient.getQueryData(["ops", "site-chats-unread"])).toBeDefined(),
    );

    state.chats = [...state.chats, chat("chat-b", "2024-01-02T00:00:00.000Z", "customer")];
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["ops", "site-chats-unread"] });
    });

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    const notice = notify.mock.calls[0][0];
    expect(notice.title).toBe("Support chat: chat-b");
    expect(notice.body).toBe("Customer is waiting");
    expect(notice.tag).toBe("support:chat-b");
    expect(notice.route).toBe("/ops?section=website&chat=chat-b");
  });

  it("does not poll or notify for a non-operator", async () => {
    const { notify } = installBridge();
    const { client } = renderAlerts({ chats: [chat("chat-a", "2024-01-01T00:00:00.000Z")] }, false);

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    expect(client.calls.filter((call) => call.path.includes("/site/chats/unread"))).toHaveLength(0);
    expect(notify).not.toHaveBeenCalled();
  });
});
