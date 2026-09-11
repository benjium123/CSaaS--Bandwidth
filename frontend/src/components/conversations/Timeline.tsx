import * as React from "react";
import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import {
  ArrowDownLeft,
  ArrowUpRight,
  PhoneMissed,
  Play,
  Voicemail,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { fetchAuthedBlob, type ApiClient } from "@/api/client";
import { cancelScheduledMessage } from "@/api/messaging";
import { isTerminalCallStatus, useCalls } from "@/api/hooks";
import { dispositionOf, type CallDisposition } from "@/api/calls";
import { DispositionPicker } from "@/components/calls/DispositionPicker";
import { AiCallCard } from "@/components/conversations/AiCallCard";
import {
  fetchConversationTimeline,
  type CallTimelineItem,
  type MessageTimelineItem,
  type NoteTimelineItem,
  type TimelineItem,
  type VoicemailTimelineItem,
} from "@/api/conversations";
import { Pill } from "@/components/ui/primitives";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import { relativeTime, statusTick } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Item 2: keeps the open thread's timeline fresh two ways - a background poll while
 * visible (TanStack pauses refetchInterval in the background by default), PLUS an
 * immediate refetch the moment a `message.received` event for THIS pair arrives over
 * the realtime socket. */
const TIMELINE_POLL_MS = 3000;

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

function MessageTimelineItemView({ item, api }: { item: MessageTimelineItem; api: ApiClient }) {
  const outbound = item.direction === "outbound";
  // Defensive on purpose: a page still holding a pre-P28 cached timeline (or a fixture
  // written before this phase) has no `links`/`clicks` at all, and a bubble must never
  // crash the whole log over a click count.
  const links = item.links ?? [];
  const clicks = item.clicks ?? 0;
  const scheduledFor = item.status === "scheduled" ? item.scheduled_for : null;
  const scheduled = Boolean(scheduledFor);
  const tick = statusTick(item.status);
  const queryClient = useQueryClient();
  const cancelSchedule = useMutation({
    mutationFn: () => cancelScheduledMessage(api, item.id),
    // P28: a scheduled message is released by the sweeper on its own clock. If this
    // mutation succeeds, the timeline entry disappears; if it 409s because the sweeper
    // already picked it up, surface the server's own message rather than inventing one.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["timeline"] });
    },
  });

  return (
    <div className={cn("flex", outbound ? "justify-end" : "justify-start")}>
      <div
        title={item.route_reason ?? undefined}
        className={cn(
          "max-w-[75%] space-y-1 rounded-lg px-3 py-2 text-sm",
          scheduled
            ? "border border-dashed border-border bg-muted text-foreground"
            : outbound
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-foreground",
        )}
      >
        {item.route_reason && <span className="sr-only">{item.route_reason}</span>}
        {scheduled && scheduledFor && (
          <p className="text-xs font-medium text-muted-foreground">
            Scheduled for {new Date(scheduledFor).toLocaleString()}
          </p>
        )}
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
        {item.failure_reason_public && (
          <p role="alert" className="text-xs text-destructive">
            {item.failure_reason_public}
          </p>
        )}
        {links.length > 0 && (
          <p className="text-[11px] text-muted-foreground">
            {clicks === 0
              ? "Link not opened yet"
              : clicks === 1
                ? "Clicked once"
                : `Clicked ${clicks}×`}
          </p>
        )}
        {scheduled && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => cancelSchedule.mutate()}
              disabled={cancelSchedule.isPending}
              aria-label="Cancel scheduled message"
              className="rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-background disabled:opacity-50"
            >
              {cancelSchedule.isPending ? "Cancelling…" : "Cancel"}
            </button>
            {cancelSchedule.isError && (
              <span role="alert" className="text-xs text-destructive">
                {(cancelSchedule.error as Error).message}
              </span>
            )}
          </div>
        )}
        <div className="flex items-center justify-end gap-2 text-[11px] opacity-70">
          <span>{relativeTime(item.occurred_at)}</span>
          {outbound && !scheduled && (
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
      const blob = await fetchAuthedBlob(api, `/api/v1/calls/${callId}/recordings/${recordingId}`);
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
        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-muted disabled:opacity-50"
      >
        <Play className="h-3 w-3" />
        {loading ? "Loading…" : "Play recording"}
      </button>
      {audioUrl && <audio ref={audioRef} src={audioUrl} controls className="h-8" />}
      {error && (
        <span role="alert" className="text-xs text-destructive">
          {error}
        </span>
      )}
    </div>
  );
}

/** P23b: a call an assistant took reads nothing like a human call - it has a summary, an
 * outcome and a transcript worth opening - so it gets its own card. The plain call card
 * below is untouched, and the human call path is unchanged. */
function CallTimelineItemView({
  item,
  api,
  ourE164,
  saved,
}: {
  item: CallTimelineItem;
  api: ApiClient;
  ourE164: string | null;
  /** P29: this call's saved result, from the calls list (the timeline payload has none).
   * Undefined until that list has loaded - no picker is offered on a guess. */
  saved: CallDisposition | undefined;
}) {
  if (item.assistant) return <AiCallCard item={{ ...item, assistant: item.assistant }} api={api} />;

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
      title={item.route_reason ?? undefined}
      className={cn(
        "flex items-center gap-3 rounded-lg border border-border bg-background px-3 py-2 text-sm",
        failed || missed ? "text-destructive" : "text-foreground",
      )}
    >
      {item.route_reason && <span className="sr-only">{item.route_reason}</span>}
      {failed || missed ? (
        <PhoneMissed className="h-4 w-4 shrink-0 text-destructive" />
      ) : item.direction === "inbound" ? (
        <ArrowDownLeft className="h-4 w-4 shrink-0 text-muted-foreground" />
      ) : (
        <ArrowUpRight className="h-4 w-4 shrink-0 text-muted-foreground" />
      )}
      <div className="min-w-0 flex-1">
        <p className="font-medium">{label}</p>
        {item.duration_seconds !== null && item.duration_seconds > 0 && (
          <p className="text-xs text-muted-foreground">{formatDuration(item.duration_seconds)}</p>
        )}
        {item.recording && (
          <CallRecordingPlayer
            api={api}
            callId={item.id}
            recordingId={item.recording.id}
            status={item.recording.status}
          />
        )}
        {ourE164 && saved && isTerminalCallStatus(item.status) && (
          <div className="text-foreground">
            <DispositionPicker
              api={api}
              callId={item.id}
              ourE164={ourE164}
              disposition={saved.disposition}
              note={saved.disposition_note}
            />
          </div>
        )}
      </div>
      <span className="ml-auto shrink-0 self-start text-[11px] text-muted-foreground">
        {relativeTime(item.occurred_at)}
      </span>
    </div>
  );
}

function VoicemailTimelineItemView({ item, api }: { item: VoicemailTimelineItem; api: ApiClient }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3 text-sm text-foreground">
      <div className="flex items-center gap-2">
        <Voicemail className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="font-medium">Voicemail</span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {relativeTime(item.occurred_at)}
        </span>
      </div>
      {item.duration_seconds !== null && item.duration_seconds > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">{formatDuration(item.duration_seconds)}</p>
      )}
      {item.transcript && (
        <p className="mt-2 whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {item.transcript}
        </p>
      )}
      {item.transcript_status === "processing" && (
        <p className="mt-2 text-xs text-muted-foreground">Transcript is processing…</p>
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

function NoteTimelineItemView({ item }: { item: NoteTimelineItem }) {
  return (
    // An <article>, not a <div>: a plain div has no role, so its aria-label is dropped by
    // the accessibility tree and a screen-reader user would meet the note with no warning
    // that it is private.
    <article
      aria-label={`Note from ${item.author_name}`}
      className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-foreground"
    >
      <div className="flex items-center gap-2">
        <Pill tone="warning">Note</Pill>
        <span className="font-medium">{item.author_name}</span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {relativeTime(item.occurred_at)}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-wrap break-words">{item.body}</p>
      {item.mentions.length > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          Mentioned: {item.mentions.map((mention) => mention.name).join(", ")}
        </p>
      )}
    </article>
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
  const softphone = useSoftphone();
  const queryClient = useQueryClient();
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
    refetchInterval: TIMELINE_POLL_MS,
  });

  React.useEffect(() => {
    if (!enabled) return undefined;
    return softphone.subscribe((event) => {
      if (event.type !== "message.received") return;
      if (event.our_e164 !== ourE164 || event.contact_e164 !== contactE164) return;
      void queryClient.invalidateQueries({ queryKey: ["timeline", contactE164, ourE164] });
    });
  }, [softphone, queryClient, enabled, contactE164, ourE164]);

  const items = React.useMemo(
    () => query.data?.pages.flatMap((page) => page.items).reverse() ?? [],
    [query.data],
  );

  // P29: the timeline payload does not carry a call's saved result, so ONE calls-list read
  // for this contact supplies it - and only once there is an ended human call to label.
  const hasEndedHumanCall = React.useMemo(
    () =>
      items.some(
        (item) => item.kind === "call" && !item.assistant && isTerminalCallStatus(item.status),
      ),
    [items],
  );
  const savedResults = useCalls(
    api,
    { contact_e164: contactE164 ?? undefined, limit: 200 },
    enabled && hasEndedHumanCall,
  );
  const savedById = React.useMemo(() => {
    const map = new Map<string, CallDisposition>();
    const rows: unknown = savedResults.data;
    if (Array.isArray(rows)) {
      rows.forEach((row: { id?: unknown }) => {
        if (typeof row?.id === "string") map.set(row.id, dispositionOf(row));
      });
    }
    return map;
  }, [savedResults.data]);

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
      <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
        Select a conversation
      </div>
    );
  }

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
        Loading timeline…
      </div>
    );
  }

  if (query.error) {
    return (
      <div className="flex h-full items-center justify-center bg-background">
        <p role="alert" className="text-sm text-destructive">
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
      <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
        No messages or calls yet
      </div>
    );
  }

  return (
    <div
      ref={scrollAreaRef}
      role="log"
      aria-live="polite"
      aria-label="Conversation timeline"
      className="min-h-0 flex-1 overflow-y-auto bg-background p-3"
    >
      <div className="space-y-4">
        {query.hasNextPage && (
          <div className="text-center">
            <button
              type="button"
              onClick={() => query.fetchNextPage()}
              disabled={query.isFetchingNextPage}
              className="rounded-md px-3 py-1 text-xs font-medium text-foreground hover:bg-muted disabled:opacity-50"
            >
              {query.isFetchingNextPage ? "Loading…" : "Load older"}
            </button>
          </div>
        )}

        {groups.map((group) => (
          <section key={group.date} className="space-y-2">
            <div className="sticky top-0 z-10 bg-background py-1 text-center text-[11px] font-medium text-muted-foreground">
              {group.label}
            </div>
            {group.items.map((item) => {
              switch (item.kind) {
                case "message":
                  return <MessageTimelineItemView key={item.id} item={item} api={api} />;
                case "call":
                  return (
                    <CallTimelineItemView
                      key={item.id}
                      item={item}
                      api={api}
                      ourE164={ourE164}
                      saved={savedById.get(item.id)}
                    />
                  );
                case "voicemail":
                  return <VoicemailTimelineItemView key={item.id} item={item} api={api} />;
                case "note":
                  return <NoteTimelineItemView key={item.id} item={item} />;
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
