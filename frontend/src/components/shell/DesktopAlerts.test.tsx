import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";
import type { Conversation } from "@/api/conversations";
import type { Notification } from "@/api/inboxPro";
import { AuthProvider } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import {
  DesktopAlerts,
  alertNotice,
  capNotices,
  conversationNotice,
  ringNotice,
} from "./DesktopAlerts";

type StubRing = {
  callId: string;
  room: string;
  from: string;
  to: string;
  kind?: "ring" | "handoff" | "invite";
  summary?: string;
  invite?: { mode: "add" | "transfer"; by: string };
};

let softphoneValue: { incoming: StubRing[] } | null = null;

// Mutable module-level value drives rings; the factory closes over it lazily.
vi.mock("@/softphone/SoftphoneProvider", () => ({
  useOptionalSoftphone: () => softphoneValue,
}));

type StubState = { conversations: Conversation[]; notifications: Notification[] };

function conv(overrides: Partial<Conversation> = {}): Conversation {
  return {
    our_e164: "+15550000001",
    contact_e164: "+15550000002",
    contact: { id: "contact-1", display_name: "Alice" },
    snippet: "hello",
    last_event_type: "message",
    direction: "inbound",
    last_event_at: "2024-01-01T00:00:00.000Z",
    unread: true,
    ...overrides,
  } as Conversation;
}

function note(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "note-1",
    kind: "missed_call",
    thread_id: "thread-1",
    our_e164: "+15550000001",
    contact_e164: "+15550000002",
    body: "Missed call",
    read_at: null,
    created_at: "2024-01-01T00:00:00.000Z",
    ...overrides,
  } as Notification;
}

function installBridge() {
  const notify = vi.fn();
  const setBadge = vi.fn();
  window.ringliteDesktop = { version: "t", platform: "win32", notify, setBadge };
  return { notify, setBadge };
}

function makeTree(client: ApiClient, queryClient: QueryClient): React.ReactElement {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <DesktopAlerts />
      </AuthProvider>
    </QueryClientProvider>
  );
}

function setup(initial: Partial<StubState> = {}) {
  const state: StubState = {
    conversations: initial.conversations ?? [],
    notifications: initial.notifications ?? [],
  };
  // A single catch-all route keeps the stub from guessing exact server paths; anything that is
  // neither conversations nor notifications (e.g. AuthProvider's /me) gets an empty object.
  const client = makeStubClient({
    "/": (path: string) => {
      if (path.includes("conversations")) {
        return { items: state.conversations, next_cursor: null };
      }
      if (path.includes("notifications")) {
        return { items: state.notifications, unread_count: state.notifications.length };
      }
      return {};
    },
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(makeTree(client, queryClient));
  return { ...utils, client, queryClient, state };
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  delete window.ringliteDesktop;
  softphoneValue = null;
});

afterEach(() => {
  cleanup();
  delete window.ringliteDesktop;
  softphoneValue = null;
});

describe("DesktopAlerts", () => {
  it("does nothing without a bridge", async () => {
    const { client } = setup({ conversations: [conv()], notifications: [note()] });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    const bridgeCalls = client.calls.filter(
      (call) => call.path.includes("conversations") || call.path.includes("notifications"),
    );
    expect(bridgeCalls).toHaveLength(0);
  });

  it("primes the first poll without notifying and sets the badge", async () => {
    const { notify, setBadge } = installBridge();
    setup({ conversations: [conv()], notifications: [note()] });

    await waitFor(() => expect(setBadge).toHaveBeenCalledWith(1));
    await flush();

    expect(notify).not.toHaveBeenCalled();
  });

  it("notifies for a new inbound message after priming", async () => {
    const { notify } = installBridge();
    const { queryClient, state } = setup({ conversations: [conv({ snippet: "old" })] });

    await waitFor(() =>
      expect(queryClient.getQueryData(["desktop-alerts", "unread-conversations"])).toBeDefined(),
    );

    state.conversations = [
      conv({ snippet: "fresh ping", last_event_at: "2024-01-02T00:00:00.000Z" }),
    ];
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["desktop-alerts", "unread-conversations"],
      });
    });

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    const notice = notify.mock.calls[0][0];
    expect(notice.title).toBe("Alice");
    expect(notice.body).toBe("fresh ping");
    expect(notice.tag).toBe("conv:+15550000001|+15550000002");
    expect(notice.route).toBe(
      "/inbox?" +
        new URLSearchParams({ contact: "+15550000002", our: "+15550000001" }).toString(),
    );
  });

  it("ignores inbound calls and outbound conversations", async () => {
    const { notify, setBadge } = installBridge();
    const { queryClient, state } = setup({ conversations: [] });

    await waitFor(() => expect(setBadge).toHaveBeenCalledWith(0));

    state.conversations = [
      conv({
        contact_e164: "+15550000010",
        contact: { id: "c10", display_name: "Caller" },
        last_event_type: "call",
        direction: "inbound",
        last_event_at: "2024-02-01T00:00:00.000Z",
      }),
      conv({
        contact_e164: "+15550000011",
        contact: { id: "c11", display_name: "Texter" },
        last_event_type: "message",
        direction: "outbound",
        last_event_at: "2024-02-01T00:00:00.000Z",
      }),
    ];
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["desktop-alerts", "unread-conversations"],
      });
    });

    await waitFor(() => expect(setBadge).toHaveBeenCalledWith(2));
    expect(notify).not.toHaveBeenCalled();
  });

  it("labels a voicemail", async () => {
    const { notify, setBadge } = installBridge();
    const { queryClient, state } = setup({ conversations: [] });

    await waitFor(() => expect(setBadge).toHaveBeenCalledWith(0));

    state.conversations = [
      conv({
        contact_e164: "+15550000020",
        last_event_type: "voicemail",
        direction: "inbound",
        snippet: "should be ignored",
        last_event_at: "2024-03-01T00:00:00.000Z",
      }),
    ];
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["desktop-alerts", "unread-conversations"],
      });
    });

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    expect(notify.mock.calls[0][0].body).toBe("New voicemail");
  });

  it("caps a burst of new conversations at three, ending with a summary", async () => {
    const { notify, setBadge } = installBridge();
    const { queryClient, state } = setup({ conversations: [] });

    await waitFor(() => expect(setBadge).toHaveBeenCalledWith(0));

    state.conversations = [1, 2, 3, 4, 5].map((n) =>
      conv({
        contact_e164: "+1555000010" + n,
        contact: { id: "c" + n, display_name: "Person " + n },
        snippet: "ping " + n,
        last_event_at: "2024-04-0" + n + "T00:00:00.000Z",
      }),
    );
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["desktop-alerts", "unread-conversations"],
      });
    });

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(3));
    const last = notify.mock.calls[2][0];
    expect(last.title).toBe("Ringlite");
    expect(last.body).toBe("5 new conversations");
    expect(last.tag).toBe("conv:summary");
  });

  it("notifies for a new unread notification after priming", async () => {
    const { notify } = installBridge();
    const { queryClient, state } = setup({ notifications: [] });

    await waitFor(() =>
      expect(queryClient.getQueryData(["desktop-alerts", "unread-notifications"])).toBeDefined(),
    );

    state.notifications = [
      note({ id: "note-2", kind: "missed_call", body: "Missed call from +1555..." }),
    ];
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["desktop-alerts", "unread-notifications"],
      });
    });

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    const notice = notify.mock.calls[0][0];
    expect(notice.title).toBe("Missed call");
    expect(notice.body).toBe("Missed call from +1555...");
    expect(notice.tag).toBe("note:note-2");
    expect(notice.route).toBe(
      "/inbox?" +
        new URLSearchParams({ contact: "+15550000002", our: "+15550000001" }).toString(),
    );
  });

  it("notifies once for an incoming ring, even across re-renders", async () => {
    const { notify } = installBridge();
    softphoneValue = {
      incoming: [
        {
          callId: "call-1",
          room: "room-1",
          from: "+15551234567",
          to: "+15559876543",
          kind: "ring",
        },
      ],
    };
    const { client, queryClient, rerender } = setup();

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    expect(notify.mock.calls[0][0].title).toBe("Incoming call");
    expect(notify.mock.calls[0][0].tag).toBe("call:call-1");

    // A fresh array identity (as a live provider would hand over) must not re-notify.
    softphoneValue = {
      incoming: [
        {
          callId: "call-1",
          room: "room-1",
          from: "+15551234567",
          to: "+15559876543",
          kind: "ring",
        },
      ],
    };
    rerender(makeTree(client, queryClient));
    await flush();

    expect(notify).toHaveBeenCalledTimes(1);
  });

  it("announces a transfer invite", async () => {
    const { notify } = installBridge();
    softphoneValue = {
      incoming: [
        {
          callId: "call-9",
          room: "room-9",
          from: "+15551230000",
          to: "+15559990000",
          kind: "invite",
          invite: { mode: "transfer", by: "Dana" },
        },
      ],
    };
    setup();

    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    expect(notify.mock.calls[0][0].title).toBe("Call transfer");
    expect(notify.mock.calls[0][0].body).toBe("From Dana");
  });

  it("never lets a throwing bridge break the console", async () => {
    const { notify } = installBridge();
    notify.mockImplementation(() => {
      throw new Error("bridge exploded");
    });
    softphoneValue = {
      incoming: [
        {
          callId: "call-boom",
          room: "room-b",
          from: "+15550000000",
          to: "+15550000009",
          kind: "ring",
        },
      ],
    };

    expect(() => setup()).not.toThrow();
    await flush();
    expect(notify).toHaveBeenCalled();
  });
});

describe("DesktopAlerts helpers", () => {
  it("builds ring notices per kind", () => {
    expect(
      ringNotice({
        callId: "c1",
        from: "+15551234567",
        kind: "invite",
        invite: { mode: "add", by: "Ann" },
      }),
    ).toEqual({ title: "Join a call", body: "From Ann", tag: "call:c1", route: "/inbox" });

    expect(
      ringNotice({
        callId: "c2",
        from: "+15551234567",
        kind: "invite",
        invite: { mode: "transfer", by: "" },
      }).title,
    ).toBe("Call transfer");

    expect(
      ringNotice({ callId: "c3", from: "+15551234567", kind: "handoff", summary: "Warm lead" }),
    ).toEqual({ title: "AI handoff", body: "Warm lead", tag: "call:c3", route: "/inbox" });
  });

  it("builds conversation notices", () => {
    const notice = conversationNotice(
      conv({
        our_e164: "+15550000001",
        contact_e164: "+15550000002",
        contact: { id: "c", display_name: "Alice" },
        snippet: "hey there",
        last_event_at: "2024-05-01T00:00:00.000Z",
      }),
    );
    expect(notice).toEqual({
      title: "Alice",
      body: "hey there",
      tag: "conv:+15550000001|+15550000002",
      route:
        "/inbox?" +
        new URLSearchParams({ contact: "+15550000002", our: "+15550000001" }).toString(),
    });
  });

  it("builds alert notices", () => {
    const notice = alertNotice(
      note({
        id: "n5",
        kind: "missed_call",
        body: "missed",
        our_e164: "+15550000001",
        contact_e164: "+15550000002",
      }),
    );
    expect(notice.title).toBe("Missed call");
    expect(notice.body).toBe("missed");
    expect(notice.tag).toBe("note:n5");
    expect(notice.route).toBe(
      "/inbox?" +
        new URLSearchParams({ contact: "+15550000002", our: "+15550000001" }).toString(),
    );
    expect(alertNotice(note({ our_e164: null, contact_e164: null })).route).toBe("/inbox");
  });

  it("caps notice lists at three and appends a summary", () => {
    const summary = vi.fn((n: number) => ({ title: "Ringlite", body: n + " new" }));
    const capped = capNotices(
      [1, 2, 3, 4, 5].map((n) => ({ title: String(n), body: "" })),
      summary,
    );
    expect(capped).toHaveLength(3);
    expect(capped[2]).toEqual({ title: "Ringlite", body: "5 new" });
    expect(summary).toHaveBeenCalledWith(5);

    expect(capNotices([{ title: "a", body: "" }], summary)).toHaveLength(1);
    expect(
      capNotices(
        [1, 2, 3].map((n) => ({ title: String(n), body: "" })),
        summary,
      ),
    ).toHaveLength(3);
  });
});
