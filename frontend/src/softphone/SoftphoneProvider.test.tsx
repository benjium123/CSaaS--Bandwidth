import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { SoftphoneProvider, useSoftphone } from "./SoftphoneProvider";
import { useAuth } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * livekit-client is mocked entirely - a fake Room recording connect/disconnect/
 * setMicrophoneEnabled calls, matching this file's own event-name strings so the
 * provider's `room.on(RoomEvent.X, ...)` wiring is exercised for real.
 */
const {
  FakeRoom,
  FakeLocalParticipant,
  FakeRemoteTrack,
  RoomEventMock,
  ConnectionStateMock,
  TrackMock,
} = vi.hoisted(() => {
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

    /** Item 3.11: a bare-bones stand-in for livekit-client's RemoteTrack, just enough to
     * exercise attach()/detach() the way the provider actually calls them. */
    class FakeRemoteTrack {
      kind: string;
      sid: string;
      private attached: HTMLMediaElement[] = [];
      constructor(kind: string, sid: string) {
        this.kind = kind;
        this.sid = sid;
      }
      attach(el?: HTMLMediaElement): HTMLMediaElement {
        const element = el ?? document.createElement("audio");
        this.attached.push(element);
        return element;
      }
      detach(el?: HTMLMediaElement): HTMLMediaElement | HTMLMediaElement[] {
        if (el) {
          this.attached = this.attached.filter((e) => e !== el);
          return el;
        }
        const all = this.attached;
        this.attached = [];
        return all;
      }
    }

    class FakeRoom {
      static instances: FakeRoom[] = [];
      /** When true, connect() stays pending until releaseConnect() - a slow ICE connect. */
      static holdConnects = false;
      private pendingConnect: { resolve: () => void; reject: (err: Error) => void } | null = null;
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
        if (!FakeRoom.holdConnects) return;
        await new Promise<void>((resolve, reject) => {
          this.pendingConnect = { resolve, reject };
        });
      }
      releaseConnect() {
        this.pendingConnect?.resolve();
        this.pendingConnect = null;
      }
      async disconnect() {
        // livekit-client rejects an in-flight connect() once the room is disconnected.
        this.pendingConnect?.reject(new Error("Client initiated disconnect"));
        this.pendingConnect = null;
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
      TrackUnsubscribed: "trackUnsubscribed",
      ParticipantDisconnected: "participantDisconnected",
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

    return {
      FakeRoom,
      FakeLocalParticipant,
      FakeRemoteTrack,
      RoomEventMock,
      ConnectionStateMock,
      TrackMock,
    };
  });

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
  onclose: ((ev?: { code?: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(_data: string) {
    /* no-op */
  }
  close(code?: number) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.(code === undefined ? undefined : { code });
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
  const { selectOrg, me } = useAuth();
  const [dialError, setDialError] = React.useState("");
  return (
    <div>
      <div data-testid="status">{sp.status}</div>
      <div data-testid="dial-error">{dialError}</div>
      <div data-testid="active-call">
        {sp.activeCall ? `${sp.activeCall.id}:${sp.activeCall.room}:${sp.activeCall.contact}` : ""}
      </div>
      <div data-testid="muted">{String(sp.muted)}</div>
      <div data-testid="device-error">{sp.deviceError ?? ""}</div>
      <div data-testid="has-me">{String(Boolean(me))}</div>
      <button onClick={() => selectOrg("org-2")}>SwitchOrg</button>
      <ul>
        {sp.incoming.map((r) => (
          <li key={r.callId} data-testid={`ring-${r.callId}`}>
            {r.from}
          </li>
        ))}
      </ul>
      <button
        onClick={() =>
          sp.dial("+19725550199", "+12145550100").catch((err: Error) => setDialError(err.message))
        }
      >
        Dial
      </button>
      <button
        onClick={() =>
          sp.hangUp().catch(() => {
            /* asserted via active-call/status staying put */
          })
        }
      >
        HangUp
      </button>
      <button onClick={() => sp.setMuted(!sp.muted).catch(() => {})}>ToggleMute</button>
      {sp.incoming.map((r) => (
        <button
          key={r.callId}
          onClick={() =>
            sp.answer(r.callId).catch(() => {
              /* asserted via the ring staying/leaving the incoming list */
            })
          }
        >
          Answer-{r.callId}
        </button>
      ))}
      {sp.incoming.map((r) => (
        <button
          key={r.callId}
          onClick={() =>
            sp.decline(r.callId).catch(() => {
              /* asserted via the ring staying/leaving the incoming list */
            })
          }
        >
          Decline-{r.callId}
        </button>
      ))}
    </div>
  );
}

const DIAL_RESPONSE = {
  id: "call-1",
  contact_e164: "+19725550199",
  status: "queued",
  room: "call-call-1",
  token: "tok-abc",
  url: "wss://lk.example.com",
  ...CALL_DETAIL_BASE,
};

function dialRoutes(extra: Record<string, unknown> = {}) {
  return {
    // Listed first: a POST to /api/v1/calls/call-9/answer was otherwise served by the
    // "/api/v1/calls" stub below.
    ...extra,
    "/api/v1/auth/me": ME,
    "/api/v1/calls": (_path: string, init: RequestInit & { json?: unknown }) => {
      if (init.method === "POST") return DIAL_RESPONSE;
      throw new Error("unexpected request");
    },
  };
}

function renderSoftphone(client: ReturnType<typeof makeStubClient>) {
  renderWithProviders(
    <SoftphoneProvider>
      <Harness />
    </SoftphoneProvider>,
    client,
  );
}

beforeEach(() => {
  FakeRoom.instances.length = 0;
  FakeRoom.holdConnects = false;
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

  // D62: live test call 2026-09-11 - a slow ICE connect kept the call UI hidden for
  // ~15s, the operator re-clicked, a second real call was placed, and the first room's
  // late disconnect wiped the live call's audio.
  it("shows the call as soon as the dial POST returns, while the room is still connecting", async () => {
    FakeRoom.holdConnects = true;
    renderSoftphone(makeStubClient(dialRoutes()));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));
    await waitFor(() =>
      expect(screen.getByTestId("active-call").textContent).toBe("call-1:call-call-1:+19725550199"),
    );
    expect(screen.getByTestId("status").textContent).toBe("connecting");

    await act(async () => {
      FakeRoom.instances.at(-1)!.releaseConnect();
    });
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("ringing-out"));
  });

  it("refuses a second dial while the first is still connecting - no second call is placed", async () => {
    FakeRoom.holdConnects = true;
    const client = makeStubClient(dialRoutes());
    renderSoftphone(client);
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));
    await userEvent.click(screen.getByText("Dial"));

    await waitFor(() =>
      expect(screen.getByTestId("dial-error").textContent).toBe("A call is already in progress"),
    );
    const posts = client.calls.filter((c) => c.path === "/api/v1/calls" && c.init.method === "POST");
    expect(posts).toHaveLength(1);
    expect(FakeRoom.instances).toHaveLength(1);
  });

  it("hanging up while the room is still connecting tears it down, and the late connect does not bring the call back", async () => {
    FakeRoom.holdConnects = true;
    renderSoftphone(makeStubClient(dialRoutes({ "/api/v1/calls/call-1/hangup": () => ({}) })));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));
    const room = FakeRoom.instances.at(-1)!;

    await userEvent.click(screen.getByText("HangUp"));
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("idle"));
    expect(room.disconnectCalls).toBe(1);
    expect(screen.getByTestId("active-call").textContent).toBe("");
    expect(screen.getByTestId("dial-error").textContent).toBe("");
  });

  it("lands in in-call, not back on ringing-out, when the far end answers while the room is still connecting", async () => {
    FakeRoom.holdConnects = true;
    renderSoftphone(makeStubClient(dialRoutes()));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));
    act(() => {
      latestWs().onmessage?.({
        data: JSON.stringify({ type: "call.status", call_id: "call-1", status: "answered" }),
      });
    });
    expect(screen.getByTestId("status").textContent).toBe("connecting");

    await act(async () => {
      FakeRoom.instances.at(-1)!.releaseConnect();
    });
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("in-call"));
  });

  it("a room superseded while still connecting never takes over the live call or wipes its audio", async () => {
    FakeRoom.holdConnects = true;
    renderSoftphone(
      makeStubClient(
        dialRoutes({
          "/api/v1/calls/call-9/answer": (_path: string, init: RequestInit & { json?: unknown }) => {
            if (init.method === "POST") {
              return { url: "wss://lk.example.com", token: "tok-inbound", room: "call-9" };
            }
            throw new Error("unexpected request");
          },
        }),
      ),
    );
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));

    await userEvent.click(screen.getByText("Dial"));
    await waitFor(() => expect(FakeRoom.instances).toHaveLength(1));
    const staleRoom = FakeRoom.instances[0];

    FakeRoom.holdConnects = false;
    act(() => {
      latestWs().onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-9",
          room: "call-9",
          from: "+19725550111",
          to: "+12145550100",
        }),
      });
    });
    await userEvent.click(await screen.findByText("Answer-call-9"));
    await waitFor(() =>
      expect(screen.getByTestId("active-call").textContent).toBe("call-9:call-9:+19725550111"),
    );
    expect(screen.getByTestId("status").textContent).toBe("in-call");
    expect(staleRoom.disconnectCalls).toBe(1);

    const liveRoom = FakeRoom.instances.at(-1)!;
    act(() => {
      liveRoom.emit("trackSubscribed", new FakeRemoteTrack("audio", "track-phone"));
    });
    expect(document.querySelectorAll("audio")).toHaveLength(1);

    // The superseded room's own late network drop must leave the live call alone.
    act(() => {
      staleRoom.emit("disconnected");
    });
    expect(screen.getByTestId("active-call").textContent).toBe("call-9:call-9:+19725550111");
    expect(screen.getByTestId("status").textContent).toBe("in-call");
    expect(document.querySelectorAll("audio")).toHaveLength(1);
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

  // Item 5
  it("keeps the incoming ring on a failed answer, and removes it only once a retry succeeds", async () => {
    let attempt = 0;
    const client = makeStubClient({
      "/api/v1/calls/call-9/answer": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method !== "POST") throw new Error("unexpected request");
        attempt += 1;
        if (attempt === 1) return new Error("network blip");
        return { url: "wss://lk.example.com", token: "tok-inbound", room: "call-9" };
      },
      "/api/v1/auth/me": ME,
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

    // First attempt failed - the card must still be there, not silently vanished.
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("idle"));
    expect(screen.getByTestId("ring-call-9")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Answer-call-9"));
    await waitFor(() =>
      expect(screen.getByTestId("active-call").textContent).toBe("call-9:call-9:+19725550111"),
    );
    expect(screen.queryByTestId("ring-call-9")).toBeNull();
  });

  // Item 16
  it("does not flip the mute state when toggling the mic fails, and surfaces the error", async () => {
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
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));

    const room = FakeRoom.instances.at(-1)!;
    room.localParticipant.setMicrophoneEnabled = async () => {
      throw new Error("mic permission revoked");
    };

    expect(screen.getByTestId("muted").textContent).toBe("false");
    await userEvent.click(screen.getByText("ToggleMute"));

    await waitFor(() =>
      expect(screen.getByTestId("device-error").textContent).toBe("mic permission revoked"),
    );
    expect(screen.getByTestId("muted").textContent).toBe("false");
  });

  // Item 17
  it("keeps the active call up when hangup fails entirely, and clears it once retried successfully", async () => {
    let hangupAttempt = 0;
    const client = makeStubClient({
      "/api/v1/calls/call-1/hangup": () => {
        hangupAttempt += 1;
        if (hangupAttempt === 1) throw new Error("hangup failed");
        return {};
      },
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
      "/api/v1/auth/me": ME,
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
      expect(screen.getByTestId("active-call").textContent).toBe("call-1:call-call-1:+19725550199"),
    );

    const room = FakeRoom.instances.at(-1)!;
    let roomShouldFail = true;
    room.disconnect = async () => {
      if (roomShouldFail) throw new Error("room disconnect failed");
      room.disconnectCalls += 1;
      room.emit("disconnected");
    };

    await userEvent.click(screen.getByText("HangUp"));
    await waitFor(() => expect(hangupAttempt).toBe(1));
    // Both the API hangup and the room disconnect failed - the call must still show.
    expect(screen.getByTestId("active-call").textContent).toBe("call-1:call-call-1:+19725550199");
    expect(screen.getByTestId("status").textContent).not.toBe("idle");

    roomShouldFail = false;
    await userEvent.click(screen.getByText("HangUp"));
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).toBe(""));
    expect(screen.getByTestId("status").textContent).toBe("idle");
  });

  // Item 18
  it("resets activeCall/incoming/room when the org changes", async () => {
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
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));

    await userEvent.click(screen.getByText("SwitchOrg"));

    await waitFor(() => expect(screen.getByTestId("active-call").textContent).toBe(""));
    expect(screen.getByTestId("status").textContent).toBe("idle");
  });

  // Item 21
  it("shows a toast with a link into the thread on sms.handoff", async () => {
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
          type: "sms.handoff",
          thread_id: "t1",
          reason: "keyword",
          contact: "+19725550111",
        }),
      });
    });

    expect(
      await screen.findByText("AI handed off an SMS conversation with +19725550111 (keyword)"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open thread" })).toHaveAttribute(
      "href",
      "/inbox?contact=%2B19725550111",
    );
  });

  // Item 21
  it("shows a toast on queue.callback_requested", async () => {
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
        data: JSON.stringify({ type: "queue.callback_requested", call_id: "c1", queue_id: "q1" }),
      });
    });

    expect(
      await screen.findByText("A caller requested a callback from a queue"),
    ).toBeInTheDocument();
  });

  // Item 26
  it("does not reconnect on a 4401 close, and logs the user out via onUnauthorized", async () => {
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
      await vi.waitFor(() => expect(screen.getByTestId("has-me").textContent).toBe("true"));

      act(() => {
        FakeWebSocket.instances[0].close(4401);
      });

      expect(screen.getByTestId("has-me").textContent).toBe("false");

      // Well past max backoff (30s) - a 4401 must never schedule a reconnect at all.
      await act(() => vi.advanceTimersByTimeAsync(45_000));
      expect(FakeWebSocket.instances.length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  // Item 27
  it("drops incoming rings older than 60s once the socket reconnects, and refetches calls", async () => {
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
      const ws1 = FakeWebSocket.instances[0];
      act(() => {
        ws1.onopen?.();
      });

      act(() => {
        ws1.onmessage?.({
          data: JSON.stringify({
            type: "call.ring",
            call_id: "call-stale",
            room: "r",
            from: "+19725550111",
            to: "+12145550100",
          }),
        });
      });
      expect(screen.getByTestId("ring-call-stale")).toBeInTheDocument();

      // Age the ring well past the 60s staleness window.
      await act(() => vi.advanceTimersByTimeAsync(61_000));

      act(() => {
        ws1.close();
      });
      // Reconnect fires after the (reset) 1s backoff.
      await act(() => vi.advanceTimersByTimeAsync(1_001));
      expect(FakeWebSocket.instances.length).toBe(2);

      act(() => {
        FakeWebSocket.instances[1].onopen?.();
      });

      expect(screen.queryByTestId("ring-call-stale")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  // Item 5: a reconnect resync must not stop at ["calls"] - conversations, inbox, and
  // any open timeline can all have moved on while the socket was down too.
  it("invalidates conversations/inbox/timeline (not just calls) once the socket reconnects", async () => {
    vi.useFakeTimers();
    const invalidateSpy = vi.spyOn(QueryClient.prototype, "invalidateQueries");
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
        FakeWebSocket.instances[0].onopen?.();
      });
      invalidateSpy.mockClear();

      act(() => {
        FakeWebSocket.instances[0].close();
      });
      await act(() => vi.advanceTimersByTimeAsync(1_001));
      expect(FakeWebSocket.instances.length).toBe(2);

      act(() => {
        FakeWebSocket.instances[1].onopen?.();
      });

      const invalidatedKeys = invalidateSpy.mock.calls.map(
        (call) => (call[0] as { queryKey: unknown[] }).queryKey[0],
      );
      expect(invalidatedKeys).toEqual(
        expect.arrayContaining(["calls", "conversations", "inbox", "timeline"]),
      );
    } finally {
      invalidateSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  // Item 3.11
  it("gives each remote participant's audio track its own <audio> element, and cleans up on unsubscribe/disconnect", async () => {
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
    await waitFor(() => expect(screen.getByTestId("active-call").textContent).not.toBe(""));

    const room = FakeRoom.instances.at(-1)!;
    const trackA = new FakeRemoteTrack("audio", "track-a");
    const trackB = new FakeRemoteTrack("audio", "track-b");

    room.emit("trackSubscribed", trackA);
    room.emit("trackSubscribed", trackB);
    // A 3-party room means two OTHER remote parties - both must be audible, so both need
    // their own element rather than the second silently stealing the first's srcObject.
    expect(document.querySelectorAll("audio")).toHaveLength(2);

    room.emit("trackUnsubscribed", trackA);
    expect(document.querySelectorAll("audio")).toHaveLength(1);

    const participantB = {
      audioTrackPublications: new Map([["pub-b", { track: trackB }]]),
    };
    room.emit("participantDisconnected", participantB);
    expect(document.querySelectorAll("audio")).toHaveLength(0);
  });

  // Item 3.12
  it("does not ring when ring_user_ids names other users but not this one, and does when it does", async () => {
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
          call_id: "call-not-for-me",
          room: "r1",
          from: "+19725550111",
          to: "+12145550100",
          ring_user_ids: ["some-other-user"],
        }),
      });
    });
    // ME.id is "u1" - not in the list, so no card at all.
    expect(screen.queryByTestId("ring-call-not-for-me")).toBeNull();

    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.ring",
          call_id: "call-for-me",
          room: "r2",
          from: "+19725550111",
          to: "+12145550100",
          ring_user_ids: ["some-other-user", "u1"],
        }),
      });
    });
    expect(await screen.findByTestId("ring-call-for-me")).toBeInTheDocument();
  });

  // Item 3.10: an EXPLICIT ring group (more than one id in ring_user_ids) must never
  // reach the whole-room hangup endpoint - decline only dismisses the card locally.
  it("does not call the hangup endpoint declining a ring-group ring - it only dismisses the card locally", async () => {
    let hangupCalled = false;
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/calls/call-group/hangup": () => {
        hangupCalled = true;
        // Even if this route were called and it failed, the group-ring path must never
        // reach it in the first place.
        throw new Error("would have ended the call for the whole group");
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
          call_id: "call-group",
          room: "r1",
          from: "+19725550111",
          to: "+12145550100",
          ring_user_ids: ["u1", "some-other-user"],
        }),
      });
    });
    expect(await screen.findByTestId("ring-call-group")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Decline-call-group"));

    await waitFor(() => expect(screen.queryByTestId("ring-call-group")).toBeNull());
    expect(hangupCalled).toBe(false);
  });

  // Item 3.10 (companion, corrected): an ABSENT ring_user_ids is broadcast to every org
  // member with access to the number - it is a ring group too, just an implicit one, so
  // it must ALSO only dismiss the card locally rather than hang up the whole room. Only
  // ring_user_ids being exactly [me.id] is a positively-solo ring safe to hang up.
  it("does not call the hangup endpoint declining a plain ring with no ring_user_ids (broadcast to everyone)", async () => {
    let hangupCalled = false;
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/calls/call-broadcast/hangup": () => {
        hangupCalled = true;
        throw new Error("would have ended the call for everyone who could see it");
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
          call_id: "call-broadcast",
          room: "r1",
          from: "+19725550111",
          to: "+12145550100",
          // ring_user_ids intentionally omitted - the broadcast-to-everyone case.
        }),
      });
    });
    expect(await screen.findByTestId("ring-call-broadcast")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Decline-call-broadcast"));

    await waitFor(() => expect(screen.queryByTestId("ring-call-broadcast")).toBeNull());
    expect(hangupCalled).toBe(false);
  });
});
