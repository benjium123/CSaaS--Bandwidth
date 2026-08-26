import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
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
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

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

function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed":
    case "answered":
    case "bridged":
      return "bg-green-100 text-green-800";
    case "failed":
    case "busy":
    case "no_answer":
      return "bg-red-100 text-red-800";
    case "canceled":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-amber-100 text-amber-800";
  }
}

export function CallsPage() {
  const { api } = useAuth();
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
      const call = await placeCall.mutateAsync({ to, from: from || undefined });
      setPlacedCall(call);
      setSelectedId(call.id);
      setTo("");
    } catch (err) {
      setPlacedCall(null);
      setDialError((err as Error).message);
    }
  }

  return (
    <div className="grid h-full grid-cols-[minmax(340px,440px)_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="space-y-3 border-b border-border p-3">
          <h1 className="text-lg font-semibold">Calls</h1>
          <form className="space-y-2" onSubmit={dial}>
            <Input
              aria-label="Number to call"
              placeholder="+19725550199"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
            <div className="flex gap-2">
              <select
                aria-label="Call from"
                className="h-9 flex-1 rounded-md border border-border bg-background px-2 text-sm"
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
              <Button type="submit" disabled={!to.trim() || placeCall.isPending}>
                Place call
              </Button>
            </div>
          </form>
          {dialError && (
            <p role="alert" className="text-sm text-destructive">
              {dialError}
            </p>
          )}
          {placedCall && (
            <p className="text-xs text-muted-foreground">
              Calling {formatPhone(placedCall.contact_e164)} — status: {placedCall.status}
            </p>
          )}

          <select
            aria-label="Filter by status"
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs"
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

        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading calls" />
          ) : error ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (calls ?? []).length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No calls yet.</p>
          ) : (
            <table className="w-full text-sm" aria-label="Calls">
              <thead className="sr-only">
                <tr>
                  <th>Contact</th>
                  <th>Direction</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
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
                      "cursor-pointer hover:bg-muted",
                      c.id === selectedId && "bg-muted",
                    )}
                  >
                    <td className="px-3 py-2">{formatPhone(c.contact_e164)}</td>
                    <td className="px-2 py-2 text-center" aria-label={c.direction}>
                      {directionArrow(c.direction)}
                    </td>
                    <td className="px-2 py-2">
                      <Badge className={statusBadgeClass(c.status)}>{c.status}</Badge>
                    </td>
                    <td className="px-2 py-2 text-xs text-muted-foreground">
                      {formatDuration(c.duration_seconds)}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {formatStarted(c.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto">
        {detail ? (
          <CallDetailPanel api={api} call={detail} />
        ) : (
          <p className="p-6 text-sm text-muted-foreground">Select a call to see details.</p>
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
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-base font-semibold">{formatPhone(call.contact_e164)}</h2>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm text-muted-foreground">
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
      </div>

      {actionError && (
        <p role="alert" className="text-sm text-destructive">
          {actionError}
        </p>
      )}
      {agentNotice && <p className="text-sm text-muted-foreground">{agentNotice}</p>}

      <div className="flex flex-wrap items-end gap-2">
        <form className="flex items-end gap-2" onSubmit={doTransfer}>
          <Input
            aria-label="Transfer to"
            placeholder="+19725550199"
            value={transferTo}
            onChange={(e) => setTransferTo(e.target.value)}
            disabled={terminal}
          />
          <Button
            type="submit"
            variant="outline"
            disabled={terminal || !transferTo.trim() || transferCall.isPending}
          >
            Transfer
          </Button>
        </form>
        <Button
          type="button"
          variant="outline"
          onClick={doSendAgent}
          disabled={terminal || dispatchAgent.isPending}
        >
          Send AI agent
        </Button>
        <Button
          type="button"
          variant="destructive"
          onClick={doHangup}
          disabled={terminal || hangupCall.isPending}
        >
          Hang up
        </Button>
      </div>

      {call.transcript && call.transcript.length > 0 && (
        <div>
          <h3 className="text-sm font-medium">Transcript</h3>
          <ul className="mt-2 space-y-2" aria-label="Transcript">
            {call.transcript.map((seg, i) => (
              <li
                key={i}
                className={cn("flex", seg.role === "agent" ? "justify-start" : "justify-end")}
              >
                <div
                  className={cn(
                    "max-w-[75%] rounded-lg px-3 py-2 text-sm",
                    seg.role === "agent"
                      ? "bg-muted text-foreground"
                      : "bg-primary text-primary-foreground",
                  )}
                >
                  {seg.text}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h3 className="text-sm font-medium">Legs</h3>
        {call.legs.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">No legs yet.</p>
        ) : (
          <table className="mt-2 w-full text-xs" aria-label="Legs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="px-2 py-1 font-medium">From → To</th>
                <th className="px-2 py-1 font-medium">Status</th>
                <th className="px-2 py-1 font-medium">Reason</th>
                <th className="px-2 py-1 font-medium">AMD</th>
                <th className="px-2 py-1 font-medium">Hangup cause</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {call.legs.map((leg) => (
                <tr key={leg.id}>
                  <td className="px-2 py-1">
                    {formatPhone(leg.from_e164)} → {formatPhone(leg.to_e164)}
                  </td>
                  <td className="px-2 py-1">{leg.status}</td>
                  <td className="px-2 py-1">{leg.reason || "—"}</td>
                  <td className="px-2 py-1">{leg.amd_result ?? "—"}</td>
                  <td className="px-2 py-1">{leg.hangup_cause ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <h3 className="text-sm font-medium">Recordings</h3>
        {call.recordings.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">No recordings.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {call.recordings.map((rec) => (
              <RecordingRow key={rec.id} api={api} callId={call.id} recording={rec} />
            ))}
          </ul>
        )}
      </div>
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
      const headers = new Headers();
      if (api.auth.token) headers.set("Authorization", `Bearer ${api.auth.token}`);
      if (api.auth.orgId) headers.set("X-Org-Id", api.auth.orgId);
      const res = await fetch(path, { headers });
      if (!res.ok) throw new Error(`Failed to load recording (${res.status})`);
      const blob = await res.blob();
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
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2 text-xs">
      <Button
        type="button"
        size="sm"
        variant="outline"
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
      {error && (
        <span role="alert" className="text-destructive">
          {error}
        </span>
      )}
    </li>
  );
}
