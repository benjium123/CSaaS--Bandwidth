import * as React from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  ArrowDownLeft,
  ArrowUpRight,
  PhoneMissed,
  Play,
  Voicemail,
} from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  fetchConversationTimeline,
  type CallTimelineItem,
  type MessageTimelineItem,
  type TimelineItem,
  type VoicemailTimelineItem,
} from "@/api/conversations";
import { relativeTime, statusTick } from "@/lib/format";
import { cn } from "@/lib/utils";

function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function dayLabel(iso: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(iso));
}

function groupByDay(items: TimelineItem[]): { date: string; label: string; items: TimelineItem[] }[] {
  const groups: { date: string; label: string; items: TimelineItem[] }[] = [];
  items.forEach((item) => {
    const date = item.occurred_at.slice(0, 10);
    const label = dayLabel(item.occurred_at);
    const last = groups[groups.length - 1];
    if (last && last.date === date) {
      last.items.push(item);
    } else {
      groups.push({ date, label, items: [item] });
    }
  });
  return groups;
}

function MessageTimelineItemView({ item }: { item: MessageTimelineItem }) {
  const outbound = item.direction === "outbound";
  const tick = statusTick(item.status);
  return (
    <div className={cn("flex", outbound ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[75%] space-y-1 rounded-lg px-3 py-2 text-sm",
          outbound
            ? "bg-primary text-primary-foreground"
            : "bg-neutral-800 text-neutral-100",
        )}
      >
        {item.media && item.media.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {item.media.map((media) => (
              <img
                key={media.url}
                src={media.url}
                alt="Attachment"
                className="h-24 w-32 rounded object-cover"
              />
            ))}
          </div>
        )}
        {item.body && <p className="whitespace-pre-wrap break-words">{item.body}</p>}
        <div className="flex items-center justify-end gap-2 text-[11px] opacity-70">
          <span>{relativeTime(item.occurred_at)}</span>
          {outbound && (
            <span title={tick.label} aria-label={tick.label}>
              {tick.glyph}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** F11: reuses the same fetch-blob-and-play approach as CallsPage.tsx's RecordingRow
 * (there is no separate `url` field - the download path is always built from the call id
 * + recording id via GET /api/v1/calls/{call_id}/recordings/{recording_id}). */
function CallRecordingPlayer({
  api,
  callId,
  recordingId,
  status,
}: {
  api: ApiClient;
  callId: string;
  recordingId: string;
  status: string;
}) {
  const [audioUrl, setAudioUrl] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const audioRef = React.useRef<HTMLAudioElement>(null);

  React.useEffect(() => {
    return () => {
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
  }, [audioUrl]);

  React.useEffect(() => {
    if (audioUrl) audioRef.current?.play();
  }, [audioUrl]);

  async function play() {
    setError(null);
    if (audioUrl) {
      audioRef.current?.play();
      return;
    }
    setLoading(true);
    try {
      const path = `/api/v1/calls/${callId}/recordings/${recordingId}`;
      const headers = new Headers();
      if (api.auth.token) headers.set("Authorization", `Bearer ${api.auth.token}`);
      if (api.auth.orgId) headers.set("X-Org-Id", api.auth.orgId);
      const res = await fetch(path, { headers });
      if (!res.ok) throw new Error(`Failed to load recording (${res.status})`);
      const blob = await res.blob();
      setAudioUrl(URL.createObjectURL(blob));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-1 flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={play}
        disabled={loading || status !== "stored"}
        className="inline-flex items-center gap-1 rounded-md border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
      >
        <Play className="h-3 w-3" />
        {loading ? "Loading…" : "Play recording"}
      </button>
      {audioUrl && <audio ref={audioRef} src={audioUrl} controls className="h-8" />}
      {error && (
        <span role="alert" className="text-xs text-red-400">
          {error}
        </span>
      )}
    </div>
  );
}

function CallTimelineItemView({ item, api }: { item: CallTimelineItem; api: ApiClient }) {
  const failed = Boolean(item.failure_detail) || item.status === "failed";
  const missed = item.status === "missed";
  const label = failed
    ? `Call failed — ${item.failure_detail}`
    : missed
      ? item.direction === "inbound"
        ? "Missed call"
        : "Call missed"
      : item.direction === "inbound"
        ? "Called you"
        : "You called";

  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm",
        failed || missed ? "text-red-400" : "text-neutral-200",
      )}
    >
      {failed || missed ? (
        <PhoneMissed className="h-4 w-4 shrink-0 text-red-400" />
      ) : item.direction === "inbound" ? (
        <ArrowDownLeft className="h-4 w-4 shrink-0 text-neutral-400" />
      ) : (
        <ArrowUpRight className="h-4 w-4 shrink-0 text-neutral-400" />
      )}
      <div className="min-w-0 flex-1">
        <p className="font-medium">{label}</p>
        {item.duration_seconds !== null && item.duration_seconds > 0 && (
          <p className="text-xs text-neutral-500">{formatDuration(item.duration_seconds)}</p>
        )}
        {item.recording && (
          <CallRecordingPlayer
            api={api}
            callId={item.id}
            recordingId={item.recording.id}
            status={item.recording.status}
          />
        )}
      </div>
      <span className="ml-auto shrink-0 self-start text-[11px] text-neutral-500">
        {relativeTime(item.occurred_at)}
      </span>
    </div>
  );
}

function VoicemailTimelineItemView({ item, api }: { item: VoicemailTimelineItem; api: ApiClient }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-sm text-neutral-200">
      <div className="flex items-center gap-2">
        <Voicemail className="h-4 w-4 shrink-0 text-neutral-400" />
        <span className="font-medium">Voicemail</span>
        <span className="ml-auto text-[11px] text-neutral-500">
          {relativeTime(item.occurred_at)}
        </span>
      </div>
      {item.duration_seconds !== null && item.duration_seconds > 0 && (
        <p className="mt-1 text-xs text-neutral-500">{formatDuration(item.duration_seconds)}</p>
      )}
      {item.transcript && (
        <p className="mt-2 whitespace-pre-wrap break-words text-xs text-neutral-400">
          {item.transcript}
        </p>
      )}
      {item.transcript_status === "processing" && (
        <p className="mt-2 text-xs text-neutral-500">Transcript is processing…</p>
      )}
      {/* F11 follow-up: the backend now exposes the same {id, status, duration_seconds}
       * recording on voicemail timeline events as it does on calls - play it the same
       * way, via the voicemail's own call_id + recording.id. */}
      {item.recording && (
        <CallRecordingPlayer
          api={api}
          callId={item.call_id}
          recordingId={item.recording.id}
          status={item.recording.status}
        />
      )}
    </div>
  );
}

export function Timeline({
  contactE164,
  ourE164,
}: {
  contactE164: string | null;
  ourE164: string | null;
}) {
  const { api } = useAuth();
  const scrollAreaRef = React.useRef<HTMLDivElement>(null);

  const enabled = Boolean(contactE164 && ourE164);
  const query = useInfiniteQuery({
    queryKey: ["timeline", contactE164, ourE164],
    queryFn: async ({ pageParam }) =>
      fetchConversationTimeline(
        api,
        contactE164 as string,
        ourE164 as string,
        pageParam as string | undefined,
      ),
    enabled,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });

  const items = React.useMemo(
    () => query.data?.pages.flatMap((page) => page.items).reverse() ?? [],
    [query.data],
  );

  // F6: key the auto-scroll on the newest item's identity, not the item COUNT - "Load
  // older" grows `items.length` too, and scrolling to the bottom on that would yank the
  // view away from the older messages the user just asked to see.
  const newestItemId = items.length > 0 ? items[items.length - 1].id : null;
  React.useEffect(() => {
    const area = scrollAreaRef.current;
    if (area) area.scrollTop = area.scrollHeight;
  }, [newestItemId]);

  if (!enabled) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-900 text-sm text-neutral-400">
        Select a conversation
      </div>
    );
  }

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-900 text-sm text-neutral-400">
        Loading timeline…
      </div>
    );
  }

  if (query.error) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-900">
        <p role="alert" className="text-sm text-red-400">
          {(query.error as Error).message}
        </p>
      </div>
    );
  }

  const groups = groupByDay(items);

  // F12: an explicit empty state for a selected conversation with zero events, distinct
  // from "Select a conversation" (not enabled) and "Loading timeline…".
  if (groups.length === 0) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-900 text-sm text-neutral-400">
        No messages or calls yet
      </div>
    );
  }

  return (
    <div
      ref={scrollAreaRef}
      className="min-h-0 flex-1 overflow-y-auto bg-neutral-900 p-3"
    >
      <div className="space-y-4">
        {query.hasNextPage && (
          <div className="text-center">
            <button
              type="button"
              onClick={() => query.fetchNextPage()}
              disabled={query.isFetchingNextPage}
              className="rounded-md px-3 py-1 text-xs font-medium text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
            >
              {query.isFetchingNextPage ? "Loading…" : "Load older"}
            </button>
          </div>
        )}

        {groups.map((group) => (
          <section key={group.date} className="space-y-2">
            <div className="sticky top-0 z-10 bg-neutral-900 py-1 text-center text-[11px] font-medium text-neutral-500">
              {group.label}
            </div>
            {group.items.map((item) => {
              switch (item.kind) {
                case "message":
                  return <MessageTimelineItemView key={item.id} item={item} />;
                case "call":
                  return <CallTimelineItemView key={item.id} item={item} api={api} />;
                case "voicemail":
                  return <VoicemailTimelineItemView key={item.id} item={item} api={api} />;
                default:
                  return null;
              }
            })}
          </section>
        ))}
      </div>
    </div>
  );
}
