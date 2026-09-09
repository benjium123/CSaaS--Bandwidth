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
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import {
  ConnectionState,
  LocalParticipant,
  Room,
  RoomEvent,
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import { isTerminalCallStatus, type CallDetailOut } from "@/api/hooks";

/** How long an incoming-ring card is allowed to survive a WS reconnect gap before it's
 * treated as stale and dropped (item 27) - a ring that's been sitting unanswered across
 * a whole reconnect is almost certainly long since resolved (answered elsewhere, missed,
 * hung up) and the socket simply never told us. */
const STALE_RING_MS = 60_000;
/** How long a toast (item 21) stays up before it self-dismisses. */
const TOAST_MS = 8_000;

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
  /** Item 3.12: the backend's `call.ring` event on a ring-group/queue offer carries
   * which member(s) it's actually meant for - absent/empty means "everyone with access"
   * (a plain inbound ring, or an AI handoff). Item 3.10 also reads this to decide whether
   * declining is safe to actually hang up (a genuinely solo ring) or must only dismiss
   * the card locally (a real group, where hanging up would end the call for every other
   * member still being offered it). */
  ringUserIds?: string[];
  /** Date.now() when this ring was created - item 27: on a WS reconnect, any ring still
   * sitting here from more than STALE_RING_MS ago is dropped rather than trusted. */
  receivedAt: number;
};

export type DeviceOption = { deviceId: string; label: string };

/** Any parsed message off the realtime events websocket, raw. Consumers outside this
 * file (item 2: ConversationsPage/Timeline reacting to `message.received`) subscribe to
 * this instead of opening a second websocket. */
export type WsEvent = Record<string, unknown> & { type?: string };

export type ToastItem = {
  id: string;
  message: string;
  linkTo?: string;
  linkLabel?: string;
};

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
  toasts: ToastItem[];
  dismissToast(id: string): void;
  dial(to: string, from?: string): Promise<void>;
  answer(callId: string): Promise<void>;
  decline(callId: string): Promise<void>;
  hangUp(): Promise<void>;
  sendDtmf(digits: string): Promise<void>;
  setMuted(muted: boolean): Promise<void>;
  setAudioDevices(inputId: string | null, outputId: string | null): Promise<void>;
  refreshDevices(): Promise<void>;
  /** item 2: subscribe to every raw event off the realtime websocket (the provider
   * already owns the one connection) - returns an unsubscribe function. */
  subscribe(handler: (event: WsEvent) => void): () => void;
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
  const queryClient = useQueryClient();

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
  const [toasts, setToasts] = React.useState<ToastItem[]>([]);

  const roomRef = React.useRef<Room | null>(null);
  const activeCallRef = React.useRef<ActiveCall | null>(null);
  // Item 3.11: a single shared <audio> element meant a second remote participant's
  // TrackSubscribed silently replaced the first one's srcObject via track.attach() - only
  // one party was ever audible in a 3+-party room. Each remote audio track now gets its
  // own element (track.attach() with no argument creates one), parked in this hidden
  // container and removed again on TrackUnsubscribed/ParticipantDisconnected.
  const audioContainerRef = React.useRef<HTMLDivElement | null>(null);
  const wsRef = React.useRef<WebSocket | null>(null);
  const reconnectTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = React.useRef(RECONNECT_MIN_MS);
  const listenersRef = React.useRef<Set<(event: WsEvent) => void>>(new Set());

  React.useEffect(() => {
    activeCallRef.current = activeCall;
  }, [activeCall]);

  const subscribe = React.useCallback((handler: (event: WsEvent) => void) => {
    listenersRef.current.add(handler);
    return () => {
      listenersRef.current.delete(handler);
    };
  }, []);

  const dismissToast = React.useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const pushToast = React.useCallback((toast: Omit<ToastItem, "id">) => {
    const id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`;
    setToasts((prev) => [...prev, { id, ...toast }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, TOAST_MS);
  }, []);

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

  // P20: org switch must not leave a call, an active LiveKit room, or someone else's
  // pending rings hanging around under the new org's UI.
  const prevOrgIdRef = React.useRef(orgId);
  React.useEffect(() => {
    if (prevOrgIdRef.current === orgId) return;
    prevOrgIdRef.current = orgId;
    teardownRoom(roomRef.current);
    roomRef.current = null;
    activeCallRef.current = null;
    setActiveCall(null);
    setIncoming([]);
    setStatus("idle");
    setMutedState(false);
  }, [orgId, teardownRoom]);

  const attachRoomListeners = React.useCallback(
    (room: Room) => {
      // Item 3.11: one <audio> element PER remote audio track, not one shared element
      // for the whole call - see audioContainerRef above.
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        const el = track.attach();
        el.autoplay = true;
        audioContainerRef.current?.appendChild(el);
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        track.detach().forEach((el) => el.remove());
      });
      room.on(RoomEvent.ParticipantDisconnected, (participant: RemoteParticipant) => {
        // Belt-and-suspenders: TrackUnsubscribed normally fires for every one of a
        // participant's tracks on disconnect, but a participant that vanishes
        // ungracefully must not leave its <audio> element(s) behind either way.
        participant.audioTrackPublications?.forEach((pub: RemoteTrackPublication) => {
          pub.track?.detach().forEach((el) => el.remove());
        });
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
        // Safety net for any element a TrackUnsubscribed/ParticipantDisconnected pair
        // above missed on the way out.
        const container = audioContainerRef.current;
        while (container?.firstChild) container.removeChild(container.firstChild);
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
      // Item 5: the ring card stays up through the whole attempt - it is only removed
      // once the backend has actually confirmed the call answered. A failed POST (bad
      // network, call already resolved elsewhere, etc.) leaves it exactly where it was
      // so the operator can see the failure and retry, instead of the card silently
      // vanishing on a doomed attempt.
      const ring = incoming.find((r) => r.callId === callId);
      setStatus("connecting");
      try {
        const result = await api.request<AnswerOut>(`/api/v1/calls/${callId}/answer`, {
          method: "POST",
        });
        setIncoming((prev) => prev.filter((r) => r.callId !== callId));
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

  // Item 3: mirrors answer() - the ring card stays up until the hangup POST actually
  // resolves (or is swallowed as "already hung up") so a failed decline leaves it in
  // place for the operator to see/retry instead of silently vanishing.
  const decline = React.useCallback(
    async (callId: string) => {
      // Item 3.10: the only hangup route this app has (POST /calls/{id}/hangup) ends the
      // call for EVERY participant - for a room call it deletes the whole LiveKit room
      // (backend/app/voice_plane/service.py hangup_room_call -> end_room_call ->
      // api.delete_room()). There is no per-user "I'm declining, offer it to someone
      // else" route, so it is only safe to call when THIS user is positively the ONLY
      // possible recipient - ringUserIds is exactly [me.id]. Absent/empty ringUserIds
      // means "broadcast to every org member with access" (item 3.12) - that is a group
      // too, just an implicit one - and more than one id is an explicit ring group. Both
      // of those (and everything else) must only dismiss the card locally; hanging up
      // would end the call for every other person who might still answer it.
      const ring = incoming.find((r) => r.callId === callId);
      const isSoloRing =
        ring?.ringUserIds?.length === 1 && me != null && ring.ringUserIds[0] === me.id;
      if (!isSoloRing) {
        setIncoming((prev) => prev.filter((r) => r.callId !== callId));
        return;
      }
      await ignoreAlreadyHungUp(
        api.request(`/api/v1/calls/${callId}/hangup`, { method: "POST" }),
      );
      setIncoming((prev) => prev.filter((r) => r.callId !== callId));
    },
    [api, incoming, me],
  );

  // Item 17: local state is cleared only once the hangup attempt actually succeeds - if
  // the hangup POST (or the room disconnect) throws something other than the expected
  // "already hung up" 422, the softphone stays visibly "in-call" with a real Hang Up
  // button to retry, instead of optimistically resetting to idle while the call may
  // still be live. The caller (SoftphonePanel) catches the rejection and shows it.
  const hangUp = React.useCallback(async () => {
    const call = activeCallRef.current;
    const room = roomRef.current;
    const tasks: Promise<unknown>[] = [];
    if (call) {
      tasks.push(ignoreAlreadyHungUp(api.request(`/api/v1/calls/${call.id}/hangup`, { method: "POST" })));
    }
    if (room) tasks.push(room.disconnect());
    await Promise.all(tasks);
    roomRef.current = null;
    setActiveCall(null);
    setStatus("idle");
    setMutedState(false);
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

  // Item 16: the mic is actually toggled FIRST - `muted` only flips once that succeeds,
  // so a failure (device pulled, permission revoked mid-call) leaves the badge/icon
  // showing the mic's real, previous state instead of lying about it.
  const setMuted = React.useCallback(async (next: boolean) => {
    const room = roomRef.current;
    if (!room) {
      setMutedState(next);
      return;
    }
    try {
      await room.localParticipant.setMicrophoneEnabled(!next);
      setMutedState(next);
    } catch (err) {
      setDeviceError(err instanceof Error ? err.message : "Failed to toggle microphone");
      throw err;
    }
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
    // Item 27: only a RECONNECT (not the very first connect) should refetch the calls
    // list and prune stale rings - there's nothing stale to prune on first mount.
    let hasConnectedOnce = false;

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
        // Item 28: the reconnect backoff resets on every successful open, not just the
        // first one - each fresh drop-then-reconnect cycle starts from RECONNECT_MIN_MS
        // again instead of inheriting whatever the previous cycle backed off to.
        backoffRef.current = RECONNECT_MIN_MS;
        setWsConnected(true);

        if (hasConnectedOnce) {
          // Item 27/5: the calls list, conversations list, inbox, and open timelines may
          // all have moved on while the socket was down (answered/missed/hung up,
          // inbound SMS, a handoff - none of it delivered) - refetch every one of them,
          // and drop any incoming ring old enough that it's almost certainly stale.
          void queryClient.invalidateQueries({ queryKey: ["calls"] });
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          void queryClient.invalidateQueries({ queryKey: ["inbox"] });
          void queryClient.invalidateQueries({ queryKey: ["timeline"] });
          const cutoff = Date.now() - STALE_RING_MS;
          setIncoming((prev) => prev.filter((r) => r.receivedAt >= cutoff));
        }
        hasConnectedOnce = true;
      };
      ws.onclose = (event?: CloseEvent) => {
        setWsConnected(false);
        if (wsRef.current === ws) wsRef.current = null;
        // Item 26: 4401 is this socket's "you are no longer authorized" close code
        // (same meaning as an HTTP 401 elsewhere) - reconnecting with the same stale
        // token would just loop the same close forever, so treat it exactly like the
        // REST client's onUnauthorized instead.
        if (event?.code === 4401) {
          api.onUnauthorized?.();
          return;
        }
        scheduleReconnect();
      };
      ws.onerror = () => {
        ws.close();
      };
      ws.onmessage = (event: MessageEvent) => {
        let msg: WsEvent & {
          call_id?: string;
          status?: string;
          room?: string;
          from?: string;
          to?: string;
          reason?: string;
          summary?: string;
          contact?: string;
          thread_id?: string;
          queue_id?: string;
          ring_user_ids?: string[];
        };
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return;
        }

        // Item 11: one subscriber throwing must not stop the rest (or the rest of this
        // handler, e.g. the ring/toast logic below) from running.
        listenersRef.current.forEach((handler) => {
          try {
            handler(msg);
          } catch (err) {
            // eslint-disable-next-line no-console
            console.error("softphone: a WS event subscriber threw", err);
          }
        });

        if (msg.type === "call.ring" && msg.call_id) {
          // Item 3.12: the backend fans call.ring out to every org member with access to
          // the number (see backend/app/api/routes/softphone.py _event_visible) - it does
          // NOT itself filter by ring_user_ids (a ring-group's targeted member(s)). An
          // absent/empty list means "everyone with access" and this user should still
          // ring; a non-empty list that doesn't name this user means it's meant for
          // someone else right now and must not ring (or show a card) here at all.
          const ringUserIds = msg.ring_user_ids;
          const ringsForMe =
            !ringUserIds || ringUserIds.length === 0 || (me != null && ringUserIds.includes(me.id));
          if (!ringsForMe) return;
          const ring: IncomingRing = {
            callId: msg.call_id,
            room: msg.room ?? "",
            from: msg.from ?? "",
            to: msg.to ?? "",
            kind: "ring",
            ringUserIds,
            receivedAt: Date.now(),
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
            receivedAt: Date.now(),
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
        } else if (msg.type === "sms.handoff") {
          // Item 21: the AI handed an SMS conversation to a human - surface it as a
          // toast with a link into that thread rather than requiring someone to notice
          // it themselves in the inbox.
          const contact = msg.contact;
          pushToast({
            message: `AI handed off an SMS conversation${contact ? ` with ${contact}` : ""}${
              msg.reason ? ` (${msg.reason})` : ""
            }`,
            linkTo: contact ? `/inbox?contact=${encodeURIComponent(contact)}` : "/inbox",
            linkLabel: "Open thread",
          });
        } else if (msg.type === "queue.callback_requested") {
          pushToast({
            message: "A caller requested a callback from a queue",
            linkTo: "/queues",
            linkLabel: "View queues",
          });
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
      toasts,
      dismissToast,
      dial,
      answer,
      decline,
      hangUp,
      sendDtmf,
      setMuted,
      setAudioDevices,
      refreshDevices,
      subscribe,
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
      toasts,
      dismissToast,
      dial,
      answer,
      decline,
      hangUp,
      sendDtmf,
      setMuted,
      setAudioDevices,
      refreshDevices,
      subscribe,
    ],
  );

  return (
    <SoftphoneContext.Provider value={value}>
      {children}
      <div ref={audioContainerRef} style={{ display: "none" }} />
      {toasts.length > 0 && (
        <div
          className="fixed right-4 top-4 z-[60] flex w-80 flex-col gap-2"
          aria-live="polite"
        >
          {toasts.map((toast) => (
            <div
              key={toast.id}
              role="status"
              className="flex items-start justify-between gap-2 rounded-lg border border-border bg-background p-3 text-sm shadow-lg"
            >
              <div className="min-w-0">
                <p>{toast.message}</p>
                {toast.linkTo && (
                  <Link
                    to={toast.linkTo}
                    className="mt-1 inline-block text-xs font-medium text-primary underline"
                    onClick={() => dismissToast(toast.id)}
                  >
                    {toast.linkLabel ?? "Open"}
                  </Link>
                )}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-muted"
                onClick={() => dismissToast(toast.id)}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </SoftphoneContext.Provider>
  );
}
