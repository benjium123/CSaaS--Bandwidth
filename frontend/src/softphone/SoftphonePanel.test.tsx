import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SoftphoneProvider } from "./SoftphoneProvider";
import { SoftphonePanel } from "./SoftphonePanel";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/** Same fake Room as SoftphoneProvider.test.tsx - see that file for rationale. */
const { FakeRoom, FakeLocalParticipant, RoomEventMock, ConnectionStateMock, TrackMock } = vi.hoisted(
  () => {
    class FakeLocalParticipant {
      micEnabled = true;
      async setMicrophoneEnabled(enabled: boolean) {
        this.micEnabled = enabled;
      }
      async publishDtmf(_code: number, _digit: string) {
        /* no-op */
      }
    }

    class FakeRoom {
      static instances: FakeRoom[] = [];
      connectCalls: Array<{ url: string; token: string }> = [];
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
        this.emit("disconnected");
      }
      async switchActiveDevice() {
        return true;
      }
    }

    return {
      FakeRoom,
      FakeLocalParticipant,
      RoomEventMock: {
        TrackSubscribed: "trackSubscribed",
        ConnectionStateChanged: "connectionStateChanged",
        Disconnected: "disconnected",
        MediaDevicesError: "mediaDevicesError",
      },
      ConnectionStateMock: {
        Disconnected: "disconnected",
        Connecting: "connecting",
        Connected: "connected",
        Reconnecting: "reconnecting",
        SignalReconnecting: "signalReconnecting",
      },
      TrackMock: { Kind: { Audio: "audio", Video: "video" } },
    };
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

const NEW_CALL_DETAIL = {
  id: "call-2",
  direction: "outbound",
  contact_e164: "+19725550199",
  our_e164: "+12145550100",
  carrier: "livekit",
  status: "queued",
  tag: null,
  answered_at: null,
  ended_at: null,
  duration_seconds: null,
  created_at: new Date().toISOString(),
  legs: [],
  recordings: [],
};

beforeEach(() => {
  FakeRoom.instances.length = 0;
  FakeWebSocket.instances.length = 0;
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SoftphonePanel", () => {
  it("caller-ID picker only lists active numbers and sends the choice as `from`", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/numbers": [
        { id: "n1", e164: "+12145550100", carrier: "bandwidth", is_active: true },
        { id: "n2", e164: "+12145550111", carrier: "bandwidth", is_active: false },
      ],
      "/api/v1/calls": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          return { ...NEW_CALL_DETAIL, room: "call-call-2", token: "tok-abc", url: "wss://lk.example.com" };
        }
        throw new Error("unexpected request");
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <SoftphonePanel />
      </SoftphoneProvider>,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Open softphone" }));

    const fromSelect = (await screen.findByLabelText("Call from")) as HTMLSelectElement;
    const optionLabels = Array.from(fromSelect.options).map((o) => o.textContent);
    expect(optionLabels).toEqual(["Any active number", "(214) 555-0100"]);

    await userEvent.selectOptions(fromSelect, "+12145550100");
    await userEvent.type(screen.getByLabelText("Number to call"), "+19725550199");
    await userEvent.click(screen.getByRole("button", { name: /Call/ }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/calls" && c.init.method === "POST"),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/calls" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toEqual({
      to: "+19725550199",
      from: "+12145550100",
      via: "room",
    });
  });

  it("shows an incoming ring card and answers it", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/numbers": [],
      "/api/v1/calls/call-9/answer": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          return { url: "wss://lk.example.com", token: "tok-inbound", room: "call-9" };
        }
        throw new Error("unexpected request");
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <SoftphonePanel />
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

    expect(await screen.findByText(/Incoming call/)).toBeInTheDocument();
    expect(screen.getByText("(972) 555-0111", { exact: false })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Answer" }));

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/calls/call-9/answer")).toBe(true),
    );
    expect(await screen.findByRole("button", { name: /Hang up/ })).toBeInTheDocument();
  });

  it("shows a priority AI handoff card and joins the room via the same answer flow as a ring", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/numbers": [],
      "/api/v1/calls/call-42/answer": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          return { url: "wss://lk.example.com", token: "tok-handoff", room: "call-room-42" };
        }
        throw new Error("unexpected request");
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <SoftphonePanel />
      </SoftphoneProvider>,
      client,
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    const ws = latestWs();
    act(() => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "call.handoff",
          call_id: "call-42",
          room: "call-room-42",
          reason: "wants pricing",
          summary: "caller asked about enterprise pricing",
          contact: "+19725550111",
        }),
      });
    });

    const card = await screen.findByLabelText("AI handoff");
    expect(card).toHaveTextContent("AI handoff");
    expect(card).toHaveTextContent("(972) 555-0111");
    expect(card).toHaveTextContent("wants pricing");
    expect(card).toHaveTextContent("caller asked about enterprise pricing");
    // A plain ring's Decline button must not appear on a handoff card.
    expect(screen.queryByRole("button", { name: "Decline" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Join call" }));

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/calls/call-42/answer")).toBe(true),
    );
    expect(await screen.findByRole("button", { name: /Hang up/ })).toBeInTheDocument();
  });
});
