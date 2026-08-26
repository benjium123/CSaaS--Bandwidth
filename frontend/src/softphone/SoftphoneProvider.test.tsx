import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SoftphoneProvider, useSoftphone } from "./SoftphoneProvider";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * livekit-client is mocked entirely - a fake Room recording connect/disconnect/
 * setMicrophoneEnabled calls, matching this file's own event-name strings so the
 * provider's `room.on(RoomEvent.X, ...)` wiring is exercised for real.
 */
const { FakeRoom, FakeLocalParticipant, RoomEventMock, ConnectionStateMock, TrackMock } = vi.hoisted(
  () => {
    class FakeLocalParticipant {
      micEnabled = true;
      dtmfLog: Array<[number, string]> = [];
      async setMicrophoneEnabled(enabled: boolean) {
        this.micEnabled = enabled;
      }
      async publishDtmf(code: number, digit: string) {
        this.dtmfLog.push([code, digit]);
      }
    }

    class FakeRoom {
      static instances: FakeRoom[] = [];
      connectCalls: Array<{ url: string; token: string }> = [];
      disconnectCalls = 0;
      switchActiveDeviceCalls: Array<[string, string]> = [];
      localParticipant = new FakeLocalParticipant();
      private listeners = new Map<string, Set<(...args: unknown[]) => void>>();

      constructor() {
        FakeRoom.instances.push(this);
      }
      on(event: string, cb: (...args: unknown[]) => void) {
        if (!this.listeners.has(event)) this.listeners.set(event, new Set());
        this.listeners.get(event)!.add(cb);
        return this;
      }
      off(event: string, cb: (...args: unknown[]) => void) {
        this.listeners.get(event)?.delete(cb);
        return this;
      }
      emit(event: string, ...args: unknown[]) {
        this.listeners.get(event)?.forEach((cb) => cb(...args));
      }
      async connect(url: string, token: string) {
        this.connectCalls.push({ url, token });
      }
      async disconnect() {
        this.disconnectCalls += 1;
        this.emit("disconnected");
      }
      async switchActiveDevice(kind: string, id: string) {
        this.switchActiveDeviceCalls.push([kind, id]);
        return true;
      }
    }

    const RoomEventMock = {
      TrackSubscribed: "trackSubscribed",
      ConnectionStateChanged: "connectionStateChanged",
      Disconnected: "disconnected",
      MediaDevicesError: "mediaDevicesError",
    };
    const ConnectionStateMock = {
      Disconnected: "disconnected",
      Connecting: "connecting",
      Connected: "connected",
      Reconnecting: "reconnecting",
      SignalReconnecting: "signalReconnecting",
    };
    const TrackMock = { Kind: { Audio: "audio", Video: "video" } };

    return { FakeRoom, FakeLocalParticipant, RoomEventMock, ConnectionStateMock, TrackMock };
  },
);

vi.mock("livekit-client", () => ({
  Room: FakeRoom,
  RoomEvent: RoomEventMock,
  ConnectionState: ConnectionStateMock,
  Track: TrackMock,
  LocalParticipant: FakeLocalParticipant,
}));

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

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

const CALL_DETAIL_BASE = {
  direction: "outbound",
  our_e164: "+12145550100",
  carrier: "livekit",
  tag: null,
  answered_at: null,
  ended_at: null,
  duration_seconds: null,
  created_at: new Date().toISOString(),
  legs: [],
  recordings: [],
};

function Harness() {
  const sp = useSoftphone();
  return (
    <div>
      <div data-testid="status">{sp.status}</div>
      <div data-testid="active-call">
        {sp.activeCall ? `${sp.activeCall.id}:${sp.activeCall.room}:${sp.activeCall.contact}` : ""}
      </div>
      <ul>
        {sp.incoming.map((r) => (
          <li key={r.callId} data-testid={`ring-${r.callId}`}>
            {r.from}
          </li>
        ))}
      </ul>
      <button onClick={() => sp.dial("+19725550199", "+12145550100")}>Dial</button>
      <button onClick={() => sp.hangUp()}>HangUp</button>
      {sp.incoming.map((r) => (
        <button key={r.callId} onClick={() => sp.answer(r.callId)}>
          Answer-{r.callId}
        </button>
      ))}
    </div>
  );
}

beforeEach(() => {
  FakeRoom.instances.length = 0;
  FakeWebSocket.instances.length = 0;
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SoftphoneProvider", () => {
  it("dials via room and connects with the returned url+token", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/calls": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          return {
            id: "call-1",
            contact_e164: "+19725550199",
            status: "queued",
            room: "call-call-1",
            token: "tok-abc",
            url: "wss://lk.example.com",
            ...CALL_DETAIL_BASE,
          };
        }
        throw new Error("unexpected request");
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Harness />
      </SoftphoneProvider>,
      client,
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));

    await waitFor(() =>
      expect(screen.getByTestId("active-call").textContent).toBe(
        "call-1:call-call-1:+19725550199",
      ),
    );
    expect(screen.getByTestId("status").textContent).toBe("ringing-out");

    const room = FakeRoom.instances.at(-1)!;
    expect(room.connectCalls).toEqual([{ url: "wss://lk.example.com", token: "tok-abc" }]);
    expect(room.localParticipant.micEnabled).toBe(true);

    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/calls" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toEqual({
      to: "+19725550199",
      from: "+12145550100",
      via: "room",
    });
  });

  it("shows an incoming ring on a call.ring ws message and clears it once the call goes terminal", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderWithProviders(
      <SoftphoneProvider>
        <Harness />
      </SoftphoneProvider>,
      client,
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();

    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-9",
          room: "call-9",
          from: "+19725550111",
          to: "+12145550100",
        }),
      });
    });

    expect(await screen.findByTestId("ring-call-9")).toHaveTextContent("+19725550111");

    // a second ring for the same call must not duplicate the entry
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-9",
          room: "call-9",
          from: "+19725550111",
          to: "+12145550100",
        }),
      });
    });
    expect(screen.getAllByTestId("ring-call-9")).toHaveLength(1);

    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({ type: "call.status", call_id: "call-9", status: "no_answer" }),
      });
    });

    await waitFor(() => expect(screen.queryByTestId("ring-call-9")).toBeNull());
  });

  it("clears one incoming card (not the other) on a call.handoff.claimed ws message", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderWithProviders(
      <SoftphoneProvider>
        <Harness />
      </SoftphoneProvider>,
      client,
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();

    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-1",
          room: "call-1",
          from: "+19725550111",
          to: "+12145550100",
        }),
      });
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-2",
          room: "call-2",
          from: "+19725550222",
          to: "+12145550100",
        }),
      });
    });

    expect(await screen.findByTestId("ring-call-1")).toBeInTheDocument();
    expect(screen.getByTestId("ring-call-2")).toBeInTheDocument();

    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({ type: "call.handoff.claimed", call_id: "call-1" }),
      });
    });

    await waitFor(() => expect(screen.queryByTestId("ring-call-1")).toBeNull());
    expect(screen.getByTestId("ring-call-2")).toBeInTheDocument();
  });

  it("answers an inbound call by posting to /answer and connecting the returned room", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/calls/call-9/answer": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          return { url: "wss://lk.example.com", token: "tok-inbound", room: "call-9" };
        }
        throw new Error("unexpected request");
      },
    });
    renderWithProviders(
      <SoftphoneProvider>
        <Harness />
      </SoftphoneProvider>,
      client,
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-9",
          room: "call-9",
          from: "+19725550111",
          to: "+12145550100",
        }),
      });
    });

    await screen.findByText("Answer-call-9");
    await userEvent.click(screen.getByText("Answer-call-9"));

    await waitFor(() =>
      expect(screen.getByTestId("active-call").textContent).toBe("call-9:call-9:+19725550111"),
    );
    expect(screen.getByTestId("status").textContent).toBe("in-call");
    expect(screen.queryByTestId("ring-call-9")).toBeNull();

    const room = FakeRoom.instances.at(-1)!;
    expect(room.connectCalls).toEqual([{ url: "wss://lk.example.com", token: "tok-inbound" }]);

    const answerCall = client.calls.find((c) => c.path === "/api/v1/calls/call-9/answer");
    expect(answerCall?.init.method).toBe("POST");
  });

  it("reconnects the events websocket with capped backoff after a drop", async () => {
    vi.useFakeTimers();
    try {
      const client = makeStubClient({ "/api/v1/auth/me": ME });
      renderWithProviders(
        <SoftphoneProvider>
          <Harness />
        </SoftphoneProvider>,
        client,
      );

      await vi.waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
      act(() => {
        FakeWebSocket.instances[0].close();
      });

      // Backoff starts at 1s - nothing new before then.
      await act(() => vi.advanceTimersByTimeAsync(999));
      expect(FakeWebSocket.instances.length).toBe(1);

      await act(() => vi.advanceTimersByTimeAsync(2));
      expect(FakeWebSocket.instances.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
