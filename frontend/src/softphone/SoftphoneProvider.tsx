/**
 * P6 browser softphone media plane.
 *
 * Owns exactly ONE LiveKit Room at a time (voice only) plus the realtime events
 * websocket that rings the org for inbound room calls. Everything else (dial pad,
 * caller-ID picker, keypad) lives in SoftphonePanel and talks to this context.
 *
 * Connection state is surfaced HONESTLY (plan D17 / phase-6-plan.md deliverable 4):
 * "in-call" only means the LiveKit room is actually Connected, and a mid-call ICE
 * hiccup flips the badge to "reconnecting" rather than silently keeping a stale
 * "in-call" label up.
 */
import * as React from "react";
import {
  ConnectionState,
  LocalParticipant,
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
} from "livekit-client";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import { isTerminalCallStatus, type CallDetailOut } from "@/api/hooks";

/** Feature-detected once at module load: some older livekit-client builds don't ship SIP
 * DTMF on LocalParticipant. The keypad disables itself (with a tooltip) rather than
 * pretending to send tones that never leave the browser. */
export const DTMF_SUPPORTED =
  typeof (LocalParticipant.prototype as unknown as { publishDtmf?: unknown }).publishDtmf ===
  "function";

const DTMF_CODES: Record<string, number> = {
  "0": 0,
  "1": 1,
  "2": 2,
  "3": 3,
  "4": 4,
  "5": 5,
  "6": 6,
  "7": 7,
  "8": 8,
  "9": 9,
  "*": 10,
  "#": 11,
};

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/** Mirrors TERMINAL_CALL_STATUSES-adjacent "still ringing" set on the backend (P5) - a
 * call is "ringing-out" until it leaves this set. */
const RINGING_STATUSES = new Set(["queued", "initiated", "ringing"]);

export type SoftphoneStatus =
  | "idle"
  | "connecting"
  | "ringing-out"
  | "in-call"
  | "reconnecting";

export type ActiveCall = { id: string; room: string; contact: string };

export type IncomingRing = {
  callId: string;
  room: string;
  from: string;
  to: string;
  /** "handoff" is a P9 AI warm-transfer ring (call.handoff) - the room call already
   * exists, so `answer` joins it exactly the same way as a plain inbound ring. */
  kind?: "ring" | "handoff";
  reason?: string;
  summary?: string;
};

export type DeviceOption = { deviceId: string; label: string };

type RoomCallOut = CallDetailOut & { room: string; token: string; url: string };
type AnswerOut = { url: string; token: string; room: string };

export type SoftphoneValue = {
  status: SoftphoneStatus;
  activeCall: ActiveCall | null;
  incoming: IncomingRing[];
  muted: boolean;
  wsConnected: boolean;
  dtmfSupported: boolean;
  devices: { inputs: DeviceOption[]; outputs: DeviceOption[] };
  selectedInputId: string | null;
  selectedOutputId: string | null;
  deviceError: string | null;
  dial(to: string, from?: string): Promise<void>;
  answer(callId: string): Promise<void>;
  decline(callId: string): Promise<void>;
  hangUp(): Promise<void>;
  sendDtmf(digits: string): Promise<void>;
  setMuted(muted: boolean): Promise<void>;
  setAudioDevices(inputId: string | null, outputId: string | null): Promise<void>;
  refreshDevices(): Promise<void>;
};

const SoftphoneContext = React.createContext<SoftphoneValue | null>(null);

export function useSoftphone(): SoftphoneValue {
  const ctx = React.useContext(SoftphoneContext);
  if (!ctx) throw new Error("useSoftphone must be used inside <SoftphoneProvider>");
  return ctx;
}

/** Swallow the "already hung up" 422 the same way CallsPage's hangup button does - the
 * room disconnect still has to happen either way. */
async function ignoreAlreadyHungUp(promise: Promise<unknown>): Promise<void> {
  try {
    await promise;
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) return;
    throw err;
  }
}

export function SoftphoneProvider({ children }: { children: React.ReactNode }) {
  const { api, me, orgId } = useAuth();

  const [status, setStatus] = React.useState<SoftphoneStatus>("idle");
  const [activeCall, setActiveCall] = React.useState<ActiveCall | null>(null);
  const [incoming, setIncoming] = React.useState<IncomingRing[]>([]);
  const [muted, setMutedState] = React.useState(false);
  const [wsConnected, setWsConnected] = React.useState(false);
  const [devices, setDevices] = React.useState<{ inputs: DeviceOption[]; outputs: DeviceOption[] }>(
    { inputs: [], outputs: [] },
  );
  const [selectedInputId, setSelectedInputId] = React.useState<string | null>(null);
  const [selectedOutputId, setSelectedOutputId] = React.useState<string | null>(null);
  const [deviceError, setDeviceError] = React.useState<string | null>(null);

  const roomRef = React.useRef<Room | null>(null);
  const activeCallRef = React.useRef<ActiveCall | null>(null);
  const audioElRef = React.useRef<HTMLAudioElement | null>(null);
  const wsRef = React.useRef<WebSocket | null>(null);
  const reconnectTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = React.useRef(RECONNECT_MIN_MS);

  React.useEffect(() => {
    activeCallRef.current = activeCall;
  }, [activeCall]);

  const refreshDevices = React.useCallback(async () => {
    try {
      const list = await navigator.mediaDevices.enumerateDevices();
      setDevices({
        inputs: list
          .filter((d) => d.kind === "audioinput")
          .map((d) => ({ deviceId: d.deviceId, label: d.label || "Microphone" })),
        outputs: list
          .filter((d) => d.kind === "audiooutput")
          .map((d) => ({ deviceId: d.deviceId, label: d.label || "Speaker" })),
      });
    } catch {
      // Device labels/list are only available after mic permission is granted - a quiet
      // no-op here is correct, not an error the user needs to see.
    }
  }, []);

  const teardownRoom = React.useCallback((room: Room | null) => {
    if (!room) return;
    room.disconnect().catch(() => {
      /* already gone */
    });
  }, []);

  const attachRoomListeners = React.useCallback(
    (room: Room) => {
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind === Track.Kind.Audio && audioElRef.current) {
          track.attach(audioElRef.current);
        }
      });
      room.on(RoomEvent.ConnectionStateChanged, (state: ConnectionState) => {
        if (roomRef.current !== room) return;
        if (state === ConnectionState.Reconnecting || state === ConnectionState.SignalReconnecting) {
          setStatus("reconnecting");
        } else if (state === ConnectionState.Connected) {
          setStatus((prev) => (prev === "reconnecting" ? "in-call" : prev));
        }
      });
      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current !== room) return;
        roomRef.current = null;
        setActiveCall(null);
        setStatus("idle");
        setMutedState(false);
      });
      room.on(RoomEvent.MediaDevicesError, (err: Error) => {
        setDeviceError(err?.message ?? "Microphone/speaker error");
      });
    },
    [],
  );

  const joinRoom = React.useCallback(
    async (
      url: string,
      token: string,
      roomName: string,
      meta: { id: string; contact: string },
      initialStatus: "ringing-out" | "in-call",
    ) => {
      teardownRoom(roomRef.current);
      roomRef.current = null;

      const room = new Room();
      attachRoomListeners(room);
      await room.connect(url, token);
      roomRef.current = room;
      setMutedState(false);
      setDeviceError(null);
      try {
        await room.localParticipant.setMicrophoneEnabled(true);
      } catch {
        // RoomEvent.MediaDevicesError already surfaces this to the UI.
      }
      setActiveCall({ id: meta.id, room: roomName, contact: meta.contact });
      setStatus(initialStatus);
      void refreshDevices();
    },
    [attachRoomListeners, refreshDevices, teardownRoom],
  );

  const dial = React.useCallback(
    async (to: string, from?: string) => {
      setStatus("connecting");
      try {
        const result = await api.request<RoomCallOut>("/api/v1/calls", {
          method: "POST",
          json: { to, from, via: "room" },
        });
        await joinRoom(
          result.url,
          result.token,
          result.room,
          { id: result.id, contact: result.contact_e164 },
          "ringing-out",
        );
      } catch (err) {
        setStatus("idle");
        throw err;
      }
    },
    [api, joinRoom],
  );

  const answer = React.useCallback(
    async (callId: string) => {
      const ring = incoming.find((r) => r.callId === callId);
      setIncoming((prev) => prev.filter((r) => r.callId !== callId));
      setStatus("connecting");
      try {
        const result = await api.request<AnswerOut>(`/api/v1/calls/${callId}/answer`, {
          method: "POST",
        });
        await joinRoom(
          result.url,
          result.token,
          result.room,
          { id: callId, contact: ring?.from ?? "" },
          "in-call",
        );
      } catch (err) {
        setStatus("idle");
        throw err;
      }
    },
    [api, incoming, joinRoom],
  );

  const decline = React.useCallback(
    async (callId: string) => {
      setIncoming((prev) => prev.filter((r) => r.callId !== callId));
      await ignoreAlreadyHungUp(
        api.request(`/api/v1/calls/${callId}/hangup`, { method: "POST" }),
      );
    },
    [api],
  );

  const hangUp = React.useCallback(async () => {
    const call = activeCallRef.current;
    const room = roomRef.current;
    roomRef.current = null;
    setActiveCall(null);
    setStatus("idle");
    setMutedState(false);
    const tasks: Promise<unknown>[] = [];
    if (call) {
      tasks.push(ignoreAlreadyHungUp(api.request(`/api/v1/calls/${call.id}/hangup`, { method: "POST" })));
    }
    if (room) tasks.push(room.disconnect());
    await Promise.all(tasks);
  }, [api]);

  const sendDtmf = React.useCallback(async (digits: string) => {
    const room = roomRef.current;
    if (!room || !DTMF_SUPPORTED) return;
    for (const digit of digits) {
      const code = DTMF_CODES[digit];
      if (code === undefined) continue;
      // eslint-disable-next-line no-await-in-loop
      await room.localParticipant.publishDtmf(code, digit);
    }
  }, []);

  const setMuted = React.useCallback(async (next: boolean) => {
    setMutedState(next);
    const room = roomRef.current;
    if (room) await room.localParticipant.setMicrophoneEnabled(!next);
  }, []);

  const setAudioDevices = React.useCallback(
    async (inputId: string | null, outputId: string | null) => {
      setSelectedInputId(inputId);
      setSelectedOutputId(outputId);
      const room = roomRef.current;
      if (!room) return;
      if (inputId) await room.switchActiveDevice("audioinput", inputId);
      if (outputId) await room.switchActiveDevice("audiooutput", outputId);
    },
    [],
  );

  // Realtime events websocket: rings the org for inbound room calls, and tells us when
  // the active call's status moves off the room (answered elsewhere, hung up, failed).
  React.useEffect(() => {
    const token = api.auth.token;
    if (!me || !orgId || !token) return;

    let cancelled = false;

    function scheduleReconnect() {
      if (cancelled) return;
      reconnectTimerRef.current = setTimeout(connect, backoffRef.current);
      backoffRef.current = Math.min(backoffRef.current * 2, RECONNECT_MAX_MS);
    }

    function connect() {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/api/v1/events/ws?token=${encodeURIComponent(
        token ?? "",
      )}&org_id=${encodeURIComponent(orgId ?? "")}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        backoffRef.current = RECONNECT_MIN_MS;
        setWsConnected(true);
      };
      ws.onclose = () => {
        setWsConnected(false);
        if (wsRef.current === ws) wsRef.current = null;
        scheduleReconnect();
      };
      ws.onerror = () => {
        ws.close();
      };
      ws.onmessage = (event: MessageEvent) => {
        let msg: {
          type?: string;
          call_id?: string;
          status?: string;
          room?: string;
          from?: string;
          to?: string;
          reason?: string;
          summary?: string;
          contact?: string;
        };
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (msg.type === "call.ring" && msg.call_id) {
          const ring: IncomingRing = {
            callId: msg.call_id,
            room: msg.room ?? "",
            from: msg.from ?? "",
            to: msg.to ?? "",
            kind: "ring",
          };
          setIncoming((prev) => (prev.some((r) => r.callId === ring.callId) ? prev : [...prev, ring]));
        } else if (msg.type === "call.handoff" && msg.call_id) {
          const handoff: IncomingRing = {
            callId: msg.call_id,
            room: msg.room ?? "",
            from: msg.contact ?? "",
            to: "",
            kind: "handoff",
            reason: msg.reason ?? "",
            summary: msg.summary ?? "",
          };
          setIncoming((prev) =>
            prev.some((r) => r.callId === handoff.callId) ? prev : [...prev, handoff],
          );
        } else if (msg.type === "call.handoff.claimed" && msg.call_id) {
          // F9: someone else already answered this ring/handoff - drop it from OUR
          // incoming list too so a claimed card doesn't linger on every other
          // operator's softphone.
          setIncoming((prev) => prev.filter((r) => r.callId !== msg.call_id));
        } else if (msg.type === "call.status" && msg.call_id && msg.status) {
          const terminal = isTerminalCallStatus(msg.status);
          if (terminal) {
            setIncoming((prev) => prev.filter((r) => r.callId !== msg.call_id));
          }
          const current = activeCallRef.current;
          if (current && current.id === msg.call_id) {
            if (terminal) {
              const room = roomRef.current;
              roomRef.current = null;
              setActiveCall(null);
              setStatus("idle");
              setMutedState(false);
              teardownRoom(room);
            } else if (!RINGING_STATUSES.has(msg.status)) {
              setStatus((prev) => (prev === "ringing-out" || prev === "connecting" ? "in-call" : prev));
            }
          }
        }
      };
    }

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      wsRef.current?.close();
      wsRef.current = null;
      setWsConnected(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, orgId, api.auth.token]);

  // Belt-and-suspenders cleanup on unmount (SoftphoneProvider is app-wide and normally
  // never unmounts, but tests mount/unmount it repeatedly).
  React.useEffect(() => {
    return () => {
      teardownRoom(roomRef.current);
      roomRef.current = null;
    };
  }, [teardownRoom]);

  const value: SoftphoneValue = React.useMemo(
    () => ({
      status,
      activeCall,
      incoming,
      muted,
      wsConnected,
      dtmfSupported: DTMF_SUPPORTED,
      devices,
      selectedInputId,
      selectedOutputId,
      deviceError,
      dial,
      answer,
      decline,
      hangUp,
      sendDtmf,
      setMuted,
      setAudioDevices,
      refreshDevices,
    }),
    [
      status,
      activeCall,
      incoming,
      muted,
      wsConnected,
      devices,
      selectedInputId,
      selectedOutputId,
      deviceError,
      dial,
      answer,
      decline,
      hangUp,
      sendDtmf,
      setMuted,
      setAudioDevices,
      refreshDevices,
    ],
  );

  return (
    <SoftphoneContext.Provider value={value}>
      {children}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio ref={audioElRef} autoPlay style={{ display: "none" }} />
    </SoftphoneContext.Provider>
  );
}
