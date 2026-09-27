import * as React from "react";
import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";
import { getErrorMessage } from "@/api/contacts";
import { useCalls, type CallOut } from "@/api/hooks";
import { useAuth } from "@/auth/AuthContext";
import { Button, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

type SuperviseMode = "monitor" | "whisper" | "barge";

interface SuperviseResult {
  url: string;
  token: string;
  room: string;
}

interface ActiveSession {
  callId: string;
  mode: SuperviseMode;
}

const SUPERVISE_PATH: Record<SuperviseMode, string> = {
  monitor: "monitor",
  whisper: "whisper",
  barge: "barge",
};

const MODE_LABEL: Record<SuperviseMode, string> = {
  monitor: "Listening",
  whisper: "Whispering to agent",
  barge: "Joined",
};

const TICK_MS = 1000;

function elapsedLabel(startedAt: string | null, now: number): string {
  const startedMs = startedAt ? new Date(startedAt).getTime() : Number.NaN;
  const elapsedMs = Number.isFinite(startedMs) ? Math.max(0, now - startedMs) : 0;
  const totalSeconds = Math.floor(elapsedMs / TICK_MS);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function directionLabel(direction: CallOut["direction"]): string {
  return direction === "inbound" ? "Incoming" : "Outgoing";
}

export function LiveCallsSupervisor(): JSX.Element {
  const { api } = useAuth();
  const callsQuery = useCalls(api, { status: "answered", limit: 50 });
  const calls = callsQuery.data ?? [];

  const [now, setNow] = React.useState(() => Date.now());
  const [active, setActive] = React.useState<ActiveSession | null>(null);
  const [pending, setPending] = React.useState<ActiveSession | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const roomRef = React.useRef<Room | null>(null);
  const audioContainerRef = React.useRef<HTMLDivElement | null>(null);
  const requestIdRef = React.useRef(0);

  // The elapsed column ticks once a second while there is anything to show.
  React.useEffect(() => {
    if (calls.length === 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [calls.length]);

  const clearAudioElements = React.useCallback(() => {
    const container = audioContainerRef.current;
    while (container?.firstChild) container.removeChild(container.firstChild);
  }, []);

  const teardownRoom = React.useCallback((room: Room | null) => {
    if (!room) return;
    room.disconnect().catch(() => {
      /* already gone */
    });
  }, []);

  const stop = React.useCallback(() => {
    // Bump the request id so any in-flight start() bails out instead of reconnecting.
    requestIdRef.current += 1;
    const room = roomRef.current;
    roomRef.current = null;
    setActive(null);
    setPending(null);
    teardownRoom(room);
    clearAudioElements();
  }, [clearAudioElements, teardownRoom]);

  const attachRoomListeners = React.useCallback(
    (room: Room) => {
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        const element = track.attach();
        element.autoplay = true;
        audioContainerRef.current?.appendChild(element);
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        track.detach().forEach((element) => element.remove());
      });
      room.on(RoomEvent.Disconnected, () => {
        // A call that ends (or drops us) must not leave a stale banner behind.
        if (roomRef.current !== room) return;
        roomRef.current = null;
        setActive(null);
        clearAudioElements();
      });
    },
    [clearAudioElements],
  );

  const start = React.useCallback(
    async (call: CallOut, mode: SuperviseMode) => {
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setError(null);
      setPending({ callId: call.id, mode });
      try {
        const result = await api.request<SuperviseResult>(
          `/api/v1/calls/${call.id}/${SUPERVISE_PATH[mode]}`,
          { method: "POST" },
        );
        if (requestIdRef.current !== requestId) return;

        // Only one session at a time: drop whatever is live before connecting.
        const previous = roomRef.current;
        roomRef.current = null;
        teardownRoom(previous);
        clearAudioElements();

        const room = new Room();
        roomRef.current = room;
        attachRoomListeners(room);
        setActive({ callId: call.id, mode });

        await room.connect(result.url, result.token);
        if (requestIdRef.current !== requestId || roomRef.current !== room) {
          teardownRoom(room);
          return;
        }
        if (mode !== "monitor") {
          // Listening is receive-only; whisper and barge publish our microphone.
          await room.localParticipant.setMicrophoneEnabled(true);
        }
      } catch (err) {
        if (requestIdRef.current !== requestId) return;
        setError(getErrorMessage(err));
        const room = roomRef.current;
        roomRef.current = null;
        setActive(null);
        teardownRoom(room);
        clearAudioElements();
      } finally {
        if (requestIdRef.current === requestId) setPending(null);
      }
    },
    [api, attachRoomListeners, clearAudioElements, teardownRoom],
  );

  React.useEffect(() => {
    return () => {
      requestIdRef.current += 1;
      const room = roomRef.current;
      roomRef.current = null;
      teardownRoom(room);
      clearAudioElements();
    };
  }, [clearAudioElements, teardownRoom]);

  const activeCall = active ? calls.find((call) => call.id === active.callId) ?? null : null;

  return (
    <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <header className="mb-3">
        <h2 className="text-sm font-semibold text-foreground">Live calls</h2>
        <p className="text-xs text-muted-foreground">
          Listen in, whisper to the agent, or join a call.
        </p>
      </header>

      {active ? (
        <div className="mb-3 flex items-center justify-between gap-2 rounded-md border border-border bg-muted/40 px-3 py-2">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">{MODE_LABEL[active.mode]}</p>
            {activeCall ? (
              <p className="truncate text-xs text-muted-foreground">
                {formatPhone(activeCall.contact_e164)}
              </p>
            ) : null}
          </div>
          <Button type="button" variant="outline" size="sm" onClick={stop}>
            Stop
          </Button>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="mb-3 text-xs text-destructive">
          {error}
        </p>
      ) : null}

      {callsQuery.isLoading ? (
        <Spinner label="Loading live calls" />
      ) : calls.length === 0 ? (
        <p className="text-sm text-muted-foreground">No live calls right now.</p>
      ) : (
        <ul className="divide-y divide-border">
          {calls.map((call) => (
            <li key={call.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-foreground">
                    {formatPhone(call.contact_e164)}
                  </span>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                    {directionLabel(call.direction)}
                  </span>
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                  <span>{formatPhone(call.our_e164)}</span>
                  <span aria-hidden="true">·</span>
                  <span>{elapsedLabel(call.answered_at ?? call.created_at, now)}</span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={pending !== null}
                  onClick={() => void start(call, "monitor")}
                >
                  Listen
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={pending !== null}
                  onClick={() => void start(call, "whisper")}
                >
                  Whisper
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={pending !== null}
                  onClick={() => void start(call, "barge")}
                >
                  Join
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div ref={audioContainerRef} className="hidden" aria-hidden="true" />
    </section>
  );
}
