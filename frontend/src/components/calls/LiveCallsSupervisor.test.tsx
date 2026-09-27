import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LiveCallsSupervisor } from "./LiveCallsSupervisor";
import { getErrorMessage } from "@/api/contacts";
import { formatPhone } from "@/lib/format";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

/** Same fake Room shape as SoftphoneProvider.test.tsx - see that file for rationale. */
const { FakeRoom, RoomEventMock, TrackMock } = vi.hoisted(() => {
  class FakeLocalParticipant {
    micEnabled = false;
    micCalls: boolean[] = [];
    async setMicrophoneEnabled(enabled: boolean) {
      this.micCalls.push(enabled);
      this.micEnabled = enabled;
    }
  }

  class FakeRoom {
    static instances: FakeRoom[] = [];
    static events: string[] = [];
    connectCalls: Array<{ url: string; token: string }> = [];
    disconnectCount = 0;
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
      FakeRoom.events.push("connect");
    }
    async disconnect() {
      this.disconnectCount += 1;
      FakeRoom.events.push("disconnect");
      this.emit("disconnected");
    }
  }

  return {
    FakeRoom,
    RoomEventMock: {
      TrackSubscribed: "trackSubscribed",
      TrackUnsubscribed: "trackUnsubscribed",
      Disconnected: "disconnected",
    },
    TrackMock: { Kind: { Audio: "audio", Video: "video" } },
  };
});

vi.mock("livekit-client", () => ({
  Room: FakeRoom,
  RoomEvent: RoomEventMock,
  Track: TrackMock,
}));

type SuperviseResponse = { url: string; token: string; room: string };

const SUPERVISE: SuperviseResponse = {
  url: "wss://livekit.test",
  token: "token-1",
  room: "room-1",
};

// The exact path useCalls builds for { status: "answered", limit: 50 }.
const CALLS_PATH = "/api/v1/calls?status=answered&limit=50";

const LIVE_CALL = {
  id: "call-1",
  direction: "inbound",
  contact_e164: "+19725550199",
  our_e164: "+12145550100",
  status: "answered",
  answered_at: new Date(Date.now() - 65_000).toISOString(),
  created_at: new Date(Date.now() - 70_000).toISOString(),
};

interface BuildOptions {
  calls?: unknown[];
  supervise?: SuperviseResponse;
  superviseError?: Error;
}

/**
 * One stub for the whole /api/v1/calls prefix: GET answers the list, POST (the supervise
 * actions) answers (or throws) the LiveKit credentials. Longest-prefix-wins in the harness
 * means this single key covers both, differing on init.method.
 */
function buildClient(options: BuildOptions = {}) {
  const superviseStub: RouteStub = (_path, init) => {
    if (init.method === "POST") {
      // The harness rethrows an Error stub value, standing in for a 403 feature_disabled.
      if (options.superviseError) return options.superviseError;
      return options.supervise ?? SUPERVISE;
    }
    return options.calls ?? [];
  };

  return makeStubClient({ "/api/v1/calls": superviseStub });
}

function mount(options: BuildOptions = {}) {
  const client = buildClient(options);
  renderWithProviders(<LiveCallsSupervisor />, client);
  return client;
}

/** Wait until no supervise request is in flight (the row buttons are enabled again). */
async function waitForIdle() {
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Join" }).hasAttribute("disabled")).toBe(false);
  });
}

function postFor(client: ReturnType<typeof buildClient>, mode: string) {
  return client.calls.find((call) => call.path === `/api/v1/calls/call-1/${mode}`);
}

beforeEach(() => {
  FakeRoom.instances.length = 0;
  FakeRoom.events.length = 0;
});

afterEach(() => {
  cleanup();
});

describe("LiveCallsSupervisor", () => {
  it("renders live calls from GET /api/v1/calls?status=answered&limit=50", async () => {
    const client = mount({ calls: [LIVE_CALL] });

    expect(await screen.findByText(formatPhone(LIVE_CALL.contact_e164))).toBeTruthy();
    expect(screen.getByText("Incoming")).toBeTruthy();
    expect(client.calls.some((call) => call.path === CALLS_PATH)).toBe(true);
  });

  it("Listen POSTs monitor, connects the room, and never enables the microphone", async () => {
    const client = mount({ calls: [LIVE_CALL], supervise: SUPERVISE });

    await userEvent.click(await screen.findByRole("button", { name: "Listen" }));
    expect(await screen.findByText("Listening")).toBeTruthy();
    await waitForIdle();

    expect(postFor(client, "monitor")?.init.method).toBe("POST");
    expect(FakeRoom.instances[0]?.connectCalls).toEqual([
      { url: SUPERVISE.url, token: SUPERVISE.token },
    ]);
    expect(FakeRoom.instances[0]?.localParticipant.micCalls).toEqual([]);
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  });

  it("Whisper POSTs whisper and enables the microphone", async () => {
    const client = mount({ calls: [LIVE_CALL], supervise: SUPERVISE });

    await userEvent.click(await screen.findByRole("button", { name: "Whisper" }));
    expect(await screen.findByText("Whispering to agent")).toBeTruthy();
    await waitForIdle();

    expect(postFor(client, "whisper")?.init.method).toBe("POST");
    expect(FakeRoom.instances[0]?.localParticipant.micCalls).toEqual([true]);
  });

  it("Stop disconnects the room and clears the active banner", async () => {
    mount({ calls: [LIVE_CALL], supervise: SUPERVISE });

    await userEvent.click(await screen.findByRole("button", { name: "Listen" }));
    expect(await screen.findByText("Listening")).toBeTruthy();
    await waitForIdle();

    await userEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(FakeRoom.instances[0]?.disconnectCount).toBe(1));
    await waitFor(() => expect(screen.queryByText("Listening")).toBeNull());
  });

  it("starting Join while listening disconnects the first room before connecting the second", async () => {
    const client = mount({ calls: [LIVE_CALL], supervise: SUPERVISE });

    await userEvent.click(await screen.findByRole("button", { name: "Listen" }));
    expect(await screen.findByText("Listening")).toBeTruthy();
    await waitForIdle();

    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(await screen.findByText("Joined")).toBeTruthy();
    await waitForIdle();

    expect(postFor(client, "barge")?.init.method).toBe("POST");
    expect(FakeRoom.instances).toHaveLength(2);
    expect(FakeRoom.instances[0]?.disconnectCount).toBe(1);
    const disconnectIndex = FakeRoom.events.indexOf("disconnect");
    const secondConnectIndex = FakeRoom.events.lastIndexOf("connect");
    expect(disconnectIndex).toBeGreaterThanOrEqual(0);
    expect(disconnectIndex).toBeLessThan(secondConnectIndex);
  });

  it("shows the error from the POST in an alert", async () => {
    const failure = new Error("feature_disabled");
    mount({ calls: [LIVE_CALL], superviseError: failure });

    await userEvent.click(await screen.findByRole("button", { name: "Listen" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toContain(getErrorMessage(failure));
  });

  it("shows the empty state when there are no live calls", async () => {
    mount({ calls: [] });

    expect(await screen.findByText("No live calls right now.")).toBeTruthy();
  });
});
