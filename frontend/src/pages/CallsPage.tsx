import * as React from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { fetchAuthedBlob, type ApiClient } from "@/api/client";
import {
  useCall,
  useCalls,
  useDispatchAgent,
  useHangupCall,
  useNumbers,
  usePlaceCall,
  useTransferCall,
  isTerminalCallStatus,
  type CallDetailOut,
  type CallFilters,
  type RecordingOut,
} from "@/api/hooks";
import { fetchInboxes } from "@/api/conversations";
import { dispositionOf } from "@/api/calls";
import { DispositionPicker } from "@/components/calls/DispositionPicker";
import { RecordingDownloads } from "@/components/calls/RecordingDownloads";
import {
  ConsoleEmpty,
  InitialsAvatar,
  PageHeader,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";
import { Badge, Button, Input, Spinner, pillToneClass } from "@/components/ui/primitives";
import { PhoneNumberMenu } from "@/components/ui/PhoneNumberMenu";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";
import { MobileBack, paneClasses } from "@/components/shell/MobileBack";

const STATUS_FILTERS = [
  { key: "", label: "All" },
  { key: "queued", label: "Queued" },
  { key: "initiated", label: "Initiated" },
  { key: "ringing", label: "Ringing" },
  { key: "answered", label: "Answered" },
  { key: "bridged", label: "Bridged" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
  { key: "busy", label: "Busy" },
  { key: "no_answer", label: "No answer" },
  { key: "canceled", label: "Canceled" },
];

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatStarted(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function directionArrow(direction: string): string {
  return direction === "inbound" ? "←" : "→";
}

/** The whole call-status family, converted together onto the shared `Pill` tones so the
 * five states stay visually coherent. They were LIGHT-mode Tailwind chips
 * (bg-green-100/text-green-800 and friends) rendering inside a dark console - pale blocks
 * that no theme change could reach. success -> --cx-live, danger -> --cx-danger,
 * neutral -> muted, warning -> --cx-flag (yellow; "ringing" was amber and is not orange
 * any more). */
function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed":
    case "answered":
    case "bridged":
      return pillToneClass("success");
    case "failed":
    case "busy":
    case "no_answer":
      return pillToneClass("danger");
    case "canceled":
      return pillToneClass("neutral");
    default:
      return pillToneClass("warning");
  }
}

export function CallsPage() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const { data: numbers } = useNumbers(api);
  const [status, setStatus] = React.useState("");
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const filters: CallFilters = React.useMemo(
    () => ({ status: status || undefined, limit: 50 }),
    [status],
  );

  const { data: calls, isLoading, error } = useCalls(api, filters);
  const { data: detail } = useCall(api, selectedId);

  const [to, setTo] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [dialError, setDialError] = React.useState<string | null>(null);
  const [placedCall, setPlacedCall] = React.useState<CallDetailOut | null>(null);
  const placeCall = usePlaceCall(api);

  const activeNumbers = (numbers ?? []).filter((n) => n.is_active);

  async function dial(e: React.FormEvent) {
    e.preventDefault();
    setDialError(null);
    try {
      // Item 20: match SoftphoneProvider.dial() (softphone/SoftphoneProvider.tsx) - it
      // always places outbound calls via="room" (a LiveKit room + SIP participant). A
      // call placed here without that never gets "via":"livekit" stamped in extra{},
      // and "Send AI agent" below always fails with "Agents can only join room calls".
      const call = await placeCall.mutateAsync({ to, from: from || undefined, via: "room" });
      setPlacedCall(call);
      setSelectedId(call.id);
      setTo("");
    } catch (err) {
      setPlacedCall(null);
      setDialError((err as Error).message);
    }
  }

  const panes = paneClasses(!!selectedId);
  return (
    <div className="grid h-full grid-cols-1 md:grid-cols-[minmax(340px,440px)_1fr]">
      <aside className={`${panes.list} min-h-0 flex-col border-r border-[hsl(var(--cx-line))]`}>
        <div className="space-y-[14px] border-b border-[hsl(var(--cx-line))] p-[18px]">
          <PageHeader title="Calls" />
          <SurfaceCard className="space-y-[11px]">
            <SectionLabel>Place a call</SectionLabel>
            <form className="space-y-[11px]" onSubmit={dial}>
            <Input
              aria-label="Number to call"
              placeholder="+19725550199"
              className="h-10 rounded-[12px] px-[14px]"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
            <div className="flex gap-[11px]">
              <select
                aria-label="Call from"
                className="h-10 flex-1 rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[14px] text-[13px]"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              >
                <option value="">Any active number</option>
                {activeNumbers.map((n) => (
                  <option key={n.id} value={n.e164}>
                    {formatPhone(n.e164)}
                  </option>
                ))}
              </select>
              <Button
                type="submit"
                className="rounded-full px-[18px]"
                disabled={!to.trim() || placeCall.isPending}
              >
                Place call
              </Button>
            </div>
            </form>
            {dialError && (
              <p role="alert" className="text-[13px] text-destructive">
                {dialError}
              </p>
            )}
            {placedCall && (
              <p className="text-[12px] text-[hsl(var(--cx-muted))]">
                Calling {formatPhone(placedCall.contact_e164)} — status: {placedCall.status}
              </p>
            )}
          </SurfaceCard>

          <select
            aria-label="Filter by status"
            className="h-9 w-full rounded-full border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[14px] text-[12.5px] text-[hsl(var(--cx-subtle))]"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-[10px]">
          {isLoading ? (
            <Spinner label="Loading calls" />
          ) : error ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (calls ?? []).length === 0 ? (
            <ConsoleEmpty>No calls yet.</ConsoleEmpty>
          ) : (
            <table
              className="w-full border-separate border-spacing-y-[3px] text-[13px]"
              aria-label="Calls"
            >
              <thead className="sr-only">
                <tr>
                  <th>Contact</th>
                  <th>Direction</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              {/* The reference draws no rule between rows: a row is a rounded block that
                  lights up, so the cells carry the fill and the outer cells the radius. */}
              <tbody>
                {(calls ?? []).map((c) => (
                  <tr
                    key={c.id}
                    role="button"
                    tabIndex={0}
                    aria-current={c.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(c.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedId(c.id);
                      }
                    }}
                    className={cn(
                      "cursor-pointer transition-colors",
                      "[&>td:first-child]:rounded-l-[14px] [&>td:last-child]:rounded-r-[14px]",
                      "hover:[&>td]:bg-[hsl(var(--cx-overlay))]",
                      c.id === selectedId && "[&>td]:bg-[hsl(var(--cx-overlay))]",
                    )}
                  >
                    {/* This cell is the boundary between the phone menu and the row's own
                        select-on-click / select-on-Enter handlers. Without stopping
                        propagation, opening the menu or pressing Enter on a menu item
                        would also select the call row. */}
                    <td
                      className="px-[12px] py-[11px]"
                      onClick={(event) => event.stopPropagation()}
                      onKeyDown={(event) => event.stopPropagation()}
                    >
                      <span className="flex items-center gap-[11px]">
                      <InitialsAvatar
                        name={formatPhone(c.contact_e164)}
                        seed={c.contact_e164}
                        size="md"
                      />
                      <PhoneNumberMenu
                        e164={c.contact_e164}
                        // A call row knows which of our numbers was on the call, so a
                        // callback goes out from the same number.
                        fromE164={c.our_e164}
                        ariaLabel={`Actions for ${formatPhone(c.contact_e164)}`}
                        onText={(e164) =>
                          navigate(
                            `/inbox?compose=${encodeURIComponent(e164)}&from=${encodeURIComponent(c.our_e164)}`,
                          )
                        }
                        className="h-auto px-1 py-0 font-normal"
                      />
                      </span>
                    </td>
                    <td className="px-[8px] py-[11px] text-center" aria-label={c.direction}>
                      {directionArrow(c.direction)}
                    </td>
                    <td className="px-[8px] py-[11px]">
                      <Badge className={statusBadgeClass(c.status)}>{c.status}</Badge>
                    </td>
                    <td className="px-[8px] py-[11px] text-[12px] text-[hsl(var(--cx-subtle))]">
                      {formatDuration(c.duration_seconds)}
                    </td>
                    <td className="px-[12px] py-[11px] text-[12px] text-[hsl(var(--cx-muted))]">
                      {formatStarted(c.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>

      <section className={`${panes.detail} min-h-0 overflow-y-auto`}>
        <MobileBack label="All calls" onBack={() => setSelectedId(null)} />
        {detail ? (
          <CallDetailPanel api={api} call={detail} />
        ) : (
          <div className="p-[18px]">
            <ConsoleEmpty>Select a call to see details.</ConsoleEmpty>
          </div>
        )}
      </section>
    </div>
  );
}

function CallDetailPanel({ api, call }: { api: ApiClient; call: CallDetailOut }) {
  const transferCall = useTransferCall(api);
  const hangupCall = useHangupCall(api);
  const dispatchAgent = useDispatchAgent(api);
  const [transferTo, setTransferTo] = React.useState("");
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [agentNotice, setAgentNotice] = React.useState<string | null>(null);
  const terminal = isTerminalCallStatus(call.status);
  const saved = dispositionOf(call);

  // Item 42: viewer-role inboxes can see a call's detail but not act on it (transfer,
  // hang up, or dispatch an AI agent) - the same "viewer" gate ConversationsPage
  // already applies to the composer/call button, resolved here off the INBOX THAT
  // OWNS THIS CALL'S OWN NUMBER (call.our_e164), not whatever inbox happens to be
  // selected in some other part of the UI (this page has no inbox selector at all).
  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });
  const inboxes = inboxesQuery.data ?? [];
  const owningInbox = inboxes.find((inbox) => inbox.e164 === call.our_e164) ?? null;
  // Fails open exactly like ConversationsPage's canSend: unknown until inboxes have
  // loaded, then true only if this number's inbox says viewer, or there's no inbox
  // system at all (a legacy/no-inbox org).
  const canUse = owningInbox
    ? owningInbox.my_role !== "viewer"
    : !inboxesQuery.isLoading && inboxes.length === 0;

  async function doTransfer(e: React.FormEvent) {
    e.preventDefault();
    setActionError(null);
    try {
      await transferCall.mutateAsync({ callId: call.id, to: transferTo });
      setTransferTo("");
    } catch (err) {
      setActionError((err as Error).message);
    }
  }

  async function doHangup() {
    setActionError(null);
    try {
      await hangupCall.mutateAsync(call.id);
    } catch (err) {
      setActionError((err as Error).message);
    }
  }

  async function doSendAgent() {
    setActionError(null);
    setAgentNotice(null);
    try {
      const result = await dispatchAgent.mutateAsync({ callId: call.id });
      setAgentNotice(`AI agent joined room ${result.room}.`);
    } catch (err) {
      // Surfaced verbatim: the backend's own error (e.g. "Agents can only join room
      // calls (via=room)") is more useful here than a generic message.
      setActionError((err as Error).message);
    }
  }

  return (
    <div className="space-y-[18px] p-[18px]">
      <SurfaceCard>
        <div className="flex items-center gap-[13px]">
          <InitialsAvatar
            name={formatPhone(call.contact_e164)}
            seed={call.contact_e164}
            size="lg"
          />
          <h2 className="text-[17px] font-semibold tracking-[-0.015em]">
            {formatPhone(call.contact_e164)}
          </h2>
        </div>
        {/* The reference panel field row: muted key on the left, value right-aligned,
            a hairline between each. dt/dd are kept so this is still a definition list. */}
        <dl className="mt-[14px] grid grid-cols-2 text-[13px] [&>dd]:border-t [&>dd]:border-[hsl(var(--cx-line))] [&>dd]:py-[11px] [&>dd]:text-right [&>dt]:border-t [&>dt]:border-[hsl(var(--cx-line))] [&>dt]:py-[11px] [&>dt]:text-[hsl(var(--cx-muted))]">
          <dt>Direction</dt>
          <dd>{call.direction}</dd>
          <dt>From</dt>
          <dd>{formatPhone(call.our_e164)}</dd>
          <dt>Carrier</dt>
          <dd>{call.carrier}</dd>
          <dt>Status</dt>
          <dd>
            <Badge className={statusBadgeClass(call.status)}>{call.status}</Badge>
          </dd>
          <dt>Duration</dt>
          <dd>{formatDuration(call.duration_seconds)}</dd>
          <dt>Started</dt>
          <dd>{formatStarted(call.created_at)}</dd>
          {call.tag && (
            <>
              <dt>Tag</dt>
              <dd>{call.tag}</dd>
            </>
          )}
        </dl>
      </SurfaceCard>

      {/* P29: the call result is picked once the call is over. The picker applies the
          same number-grant rule as the buttons below, plus the catalogue read. */}
      {terminal && (
        <DispositionPicker
          key={call.id}
          api={api}
          callId={call.id}
          ourE164={call.our_e164}
          disposition={saved.disposition}
          note={saved.disposition_note}
        />
      )}

      {actionError && (
        <p role="alert" className="text-sm text-destructive">
          {actionError}
        </p>
      )}
      {agentNotice && <p className="text-sm text-muted-foreground">{agentNotice}</p>}

      {!canUse && (
        <p className="text-xs text-muted-foreground">
          Read-only inbox — you can view but not act on this call
        </p>
      )}

      <SurfaceCard className="space-y-[11px]">
        <SectionLabel>Actions</SectionLabel>
        <div className="flex flex-wrap items-end gap-[11px]">
          <form className="flex items-end gap-[11px]" onSubmit={doTransfer}>
            <Input
              aria-label="Transfer to"
              placeholder="+19725550199"
              className="h-10 rounded-[12px] px-[14px]"
              value={transferTo}
              onChange={(e) => setTransferTo(e.target.value)}
              disabled={terminal || !canUse}
            />
            <Button
              type="submit"
              variant="outline"
              className="rounded-full px-[18px]"
              disabled={terminal || !canUse || !transferTo.trim() || transferCall.isPending}
            >
              Transfer
            </Button>
          </form>
          <Button
            type="button"
            variant="outline"
            className="rounded-full px-[18px]"
            onClick={doSendAgent}
            disabled={terminal || !canUse || dispatchAgent.isPending}
          >
            Send AI agent
          </Button>
          <Button
            type="button"
            variant="destructive"
            className="rounded-full px-[18px]"
            onClick={doHangup}
            disabled={terminal || !canUse || hangupCall.isPending}
          >
            Hang up
          </Button>
        </div>
      </SurfaceCard>

      {call.transcript && call.transcript.length > 0 && (
        <SurfaceCard>
          <SectionLabel>Transcript</SectionLabel>
          <ul className="mt-[11px] space-y-[11px]" aria-label="Transcript">
            {call.transcript.map((seg, i) => (
              <li
                key={i}
                className={cn("flex", seg.role === "agent" ? "justify-start" : "justify-end")}
              >
                <div
                  className={cn(
                    "max-w-[75%] rounded-[18px] px-[15px] py-[11px] text-[14px] leading-[1.55]",
                    seg.role === "agent"
                      ? "rounded-bl-[6px] bg-[hsl(var(--cx-overlay))] text-[hsl(var(--cx-text))]"
                      : "rounded-br-[6px] bg-[hsl(var(--cx-accent))] text-[hsl(var(--cx-on-acc))]",
                  )}
                >
                  {seg.text}
                </div>
              </li>
            ))}
          </ul>
        </SurfaceCard>
      )}

      <SurfaceCard>
        <SectionLabel>Recordings</SectionLabel>
        {call.recordings.length === 0 ? (
          <p className="mt-[11px] text-[12px] text-[hsl(var(--cx-muted))]">No recordings.</p>
        ) : (
          <ul className="mt-[11px] space-y-[11px]">
            {call.recordings.map((rec) => (
              <RecordingRow key={rec.id} api={api} callId={call.id} recording={rec} />
            ))}
          </ul>
        )}
      </SurfaceCard>
    </div>
  );
}

function RecordingRow({
  api,
  callId,
  recording,
}: {
  api: ApiClient;
  callId: string;
  recording: RecordingOut;
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

  async function play() {
    setError(null);
    if (audioUrl) {
      audioRef.current?.play();
      return;
    }
    setLoading(true);
    try {
      const path = recording.url ?? `/api/v1/calls/${callId}/recordings/${recording.id}`;
      const blob = await fetchAuthedBlob(api, path);
      const url = URL.createObjectURL(blob);
      setAudioUrl(url);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    if (audioUrl) audioRef.current?.play();
  }, [audioUrl]);

  return (
    <li className="flex flex-wrap items-center gap-[11px] rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px] text-[12px]">
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="rounded-full px-[13px]"
        onClick={play}
        disabled={loading || recording.status !== "stored"}
      >
        {loading ? "Loading…" : "Play"}
      </Button>
      <span className="text-muted-foreground">
        {recording.duration_seconds != null ? formatDuration(recording.duration_seconds) : "—"}
      </span>
      <span className="text-muted-foreground">{recording.status}</span>
      {audioUrl && <audio ref={audioRef} src={audioUrl} controls className="h-8" />}
      <RecordingDownloads api={api} callId={callId} recording={recording} />
      {error && (
        <span role="alert" className="text-destructive">
          {error}
        </span>
      )}
    </li>
  );
}
