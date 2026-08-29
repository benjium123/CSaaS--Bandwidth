import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useBusinessHours,
  useCreateBusinessHours,
  useCreateQueue,
  useCreateRingGroup,
  useMarkVoicemailRead,
  useOrgMembers,
  useQueueEntries,
  useQueues,
  useRingGroups,
  useVoicemails,
  type QueueOut,
} from "@/api/hooks";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const WEEKDAYS = [
  { key: "mon", label: "Mon" },
  { key: "tue", label: "Tue" },
  { key: "wed", label: "Wed" },
  { key: "thu", label: "Thu" },
  { key: "fri", label: "Fri" },
  { key: "sat", label: "Sat" },
  { key: "sun", label: "Sun" },
] as const;

const OVERFLOW_OPTIONS = [
  { value: "voicemail", label: "Voicemail" },
  { value: "hangup", label: "Hangup" },
  { value: "callback", label: "Callback" },
];

export function QueuesPage() {
  const { api } = useAuth();
  return (
    <div className="mx-auto max-w-4xl space-y-10 overflow-y-auto p-6">
      <h1 className="text-lg font-semibold">Queues &amp; routing</h1>
      <BusinessHoursSection api={api} />
      <RingGroupsSection api={api} />
      <QueuesSection api={api} />
      <VoicemailsSection api={api} />
    </div>
  );
}

/* ---------------------------------------------------------------------------------------
 * Business hours (DR-10): per-weekday [open, close] windows + a holidays list, in one
 * IANA timezone.
 * ------------------------------------------------------------------------------------- */
function BusinessHoursSection({ api }: { api: ApiClient }) {
  const { data: hours, isLoading } = useBusinessHours(api);
  const createHours = useCreateBusinessHours(api);

  const [name, setName] = React.useState("default");
  const [timezone, setTimezone] = React.useState("America/Chicago");
  const [schedule, setSchedule] = React.useState<Record<string, [string, string][]>>({});
  const [holidayInput, setHolidayInput] = React.useState("");
  const [holidays, setHolidays] = React.useState<string[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  function windowsFor(day: string): [string, string][] {
    return schedule[day] ?? [];
  }

  function addWindow(day: string) {
    setSchedule((prev) => ({ ...prev, [day]: [...(prev[day] ?? []), ["09:00", "17:00"]] }));
  }

  function updateWindow(day: string, idx: number, pos: 0 | 1, value: string) {
    setSchedule((prev) => {
      const next = [...(prev[day] ?? [])];
      const w: [string, string] = [...next[idx]] as [string, string];
      w[pos] = value;
      next[idx] = w;
      return { ...prev, [day]: next };
    });
  }

  function removeWindow(day: string, idx: number) {
    setSchedule((prev) => ({ ...prev, [day]: (prev[day] ?? []).filter((_, i) => i !== idx) }));
  }

  function addHoliday() {
    const v = holidayInput.trim();
    if (v && !holidays.includes(v)) setHolidays((prev) => [...prev, v]);
    setHolidayInput("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createHours.mutateAsync({ name: name.trim() || "default", timezone, schedule, holidays });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold">Business hours</h2>

      {isLoading ? (
        <Spinner label="Loading business hours" />
      ) : (hours ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No business hours configured yet.</p>
      ) : (
        <ul aria-label="Business hours" className="divide-y divide-border rounded-md border border-border">
          {(hours ?? []).map((h) => (
            <li key={h.id} className="p-3 text-sm">
              <span className="font-medium">{h.name}</span>{" "}
              <span className="text-xs text-muted-foreground">
                {h.timezone} · {h.holidays.length} holiday{h.holidays.length === 1 ? "" : "s"}
              </span>
            </li>
          ))}
        </ul>
      )}

      <form className="space-y-3 rounded-md border border-border p-3" onSubmit={submit}>
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="bh-name">
              Name
            </label>
            <Input id="bh-name" aria-label="Business hours name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="bh-tz">
              Timezone (IANA)
            </label>
            <Input
              id="bh-tz"
              aria-label="Timezone"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            />
          </div>
        </div>

        <div className="space-y-2">
          {WEEKDAYS.map((day) => (
            <div key={day.key} className="flex flex-wrap items-center gap-2">
              <span className="w-10 text-xs text-muted-foreground">{day.label}</span>
              {windowsFor(day.key).map((w, i) => (
                <div key={i} className="flex items-center gap-1">
                  <Input
                    aria-label={`${day.label} window ${i + 1} open`}
                    className="h-8 w-24"
                    value={w[0]}
                    onChange={(e) => updateWindow(day.key, i, 0, e.target.value)}
                  />
                  <span className="text-xs text-muted-foreground">–</span>
                  <Input
                    aria-label={`${day.label} window ${i + 1} close`}
                    className="h-8 w-24"
                    value={w[1]}
                    onChange={(e) => updateWindow(day.key, i, 1, e.target.value)}
                  />
                  <Button type="button" size="sm" variant="ghost" onClick={() => removeWindow(day.key, i)}>
                    ×
                  </Button>
                </div>
              ))}
              <Button type="button" size="sm" variant="outline" onClick={() => addWindow(day.key)}>
                Add window
              </Button>
            </div>
          ))}
        </div>

        <div className="space-y-1">
          <span className="block text-xs text-muted-foreground">Holidays (ISO dates)</span>
          <div className="flex flex-wrap gap-1">
            {holidays.map((d) => (
              <Badge key={d} className="bg-muted text-foreground">
                {d}
                <button
                  type="button"
                  aria-label={`Remove holiday ${d}`}
                  className="ml-1"
                  onClick={() => setHolidays((prev) => prev.filter((x) => x !== d))}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>
          <div className="flex gap-2">
            <Input
              aria-label="Add holiday date"
              placeholder="2026-12-25"
              className="h-8 w-40"
              value={holidayInput}
              onChange={(e) => setHolidayInput(e.target.value)}
            />
            <Button type="button" size="sm" variant="outline" onClick={addHoliday}>
              Add holiday
            </Button>
          </div>
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <Button type="submit" size="sm" disabled={createHours.isPending}>
          Save business hours
        </Button>
      </form>
    </section>
  );
}

/* ---------------------------------------------------------------------------------------
 * Ring groups (DR-5)
 * ------------------------------------------------------------------------------------- */
function RingGroupsSection({ api }: { api: ApiClient }) {
  const { data: groups, isLoading } = useRingGroups(api);
  const { data: members } = useOrgMembers(api);
  const createGroup = useCreateRingGroup(api);

  const [name, setName] = React.useState("");
  const [strategy, setStrategy] = React.useState<"simultaneous" | "sequential">("simultaneous");
  const [memberIds, setMemberIds] = React.useState<string[]>([]);
  const [ringTimeout, setRingTimeout] = React.useState(20);
  const [error, setError] = React.useState<string | null>(null);

  function toggleMember(id: string) {
    setMemberIds((prev) => (prev.includes(id) ? prev.filter((m) => m !== id) : [...prev, id]));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createGroup.mutateAsync({
        name: name.trim(),
        strategy,
        member_user_ids: memberIds,
        ring_timeout_seconds: ringTimeout,
      });
      setName("");
      setMemberIds([]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold">Ring groups</h2>

      {isLoading ? (
        <Spinner label="Loading ring groups" />
      ) : (groups ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No ring groups yet.</p>
      ) : (
        <ul aria-label="Ring groups" className="divide-y divide-border rounded-md border border-border">
          {(groups ?? []).map((g) => (
            <li key={g.id} className="flex items-center justify-between p-3 text-sm">
              <span className="font-medium">{g.name}</span>
              <span className="text-xs text-muted-foreground">
                {g.strategy} · {g.member_user_ids.length} member{g.member_user_ids.length === 1 ? "" : "s"} ·{" "}
                {g.ring_timeout_seconds}s
              </span>
            </li>
          ))}
        </ul>
      )}

      <form className="space-y-3 rounded-md border border-border p-3" onSubmit={submit}>
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="rg-name">
              Name
            </label>
            <Input id="rg-name" aria-label="Ring group name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="rg-strategy">
              Strategy
            </label>
            <select
              id="rg-strategy"
              aria-label="Ring strategy"
              className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              value={strategy}
              onChange={(e) => setStrategy(e.target.value as "simultaneous" | "sequential")}
            >
              <option value="simultaneous">Simultaneous</option>
              <option value="sequential">Sequential</option>
            </select>
          </div>
        </div>

        <div className="space-y-1">
          <span className="block text-xs text-muted-foreground">Members</span>
          <div className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-border p-2" role="group" aria-label="Members">
            {(members ?? []).length === 0 ? (
              <p className="text-xs text-muted-foreground">No team members.</p>
            ) : (
              (members ?? []).map((m) => (
                <label key={m.user_id} className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={memberIds.includes(m.user_id)}
                    onChange={() => toggleMember(m.user_id)}
                  />
                  {m.full_name} ({m.email})
                </label>
              ))
            )}
          </div>
        </div>

        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="rg-timeout">
            Ring timeout (seconds)
          </label>
          <Input
            id="rg-timeout"
            aria-label="Ring timeout seconds"
            type="number"
            min={1}
            max={120}
            className="h-8 w-24"
            value={ringTimeout}
            onChange={(e) => setRingTimeout(Number(e.target.value))}
          />
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <Button type="submit" size="sm" disabled={!name.trim() || createGroup.isPending}>
          Create ring group
        </Button>
      </form>
    </section>
  );
}

/* ---------------------------------------------------------------------------------------
 * Queues (DR-6): CRUD + a live-ish entries list for whichever queue is expanded.
 * ------------------------------------------------------------------------------------- */
function QueuesSection({ api }: { api: ApiClient }) {
  const { data: queues, isLoading } = useQueues(api);
  const { data: ringGroups } = useRingGroups(api);
  const createQueue = useCreateQueue(api);

  const [name, setName] = React.useState("");
  const [holdAudioUrl, setHoldAudioUrl] = React.useState("");
  const [maxWaitSeconds, setMaxWaitSeconds] = React.useState(300);
  const [overflow, setOverflow] = React.useState("voicemail");
  const [ringGroupId, setRingGroupId] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [expandedId, setExpandedId] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createQueue.mutateAsync({
        name: name.trim(),
        hold_audio_url: holdAudioUrl.trim() || null,
        max_wait_seconds: maxWaitSeconds,
        overflow,
        ring_group_id: ringGroupId || null,
      });
      setName("");
      setHoldAudioUrl("");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold">Queues</h2>

      {isLoading ? (
        <Spinner label="Loading queues" />
      ) : (queues ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No queues yet.</p>
      ) : (
        <ul aria-label="Queues" className="divide-y divide-border rounded-md border border-border">
          {(queues ?? []).map((q) => (
            <li key={q.id} className="p-3 text-sm">
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 text-left"
                onClick={() => setExpandedId((prev) => (prev === q.id ? null : q.id))}
              >
                <span className="font-medium">{q.name}</span>
                <span className="text-xs text-muted-foreground">
                  overflow: {q.overflow} · max wait {q.max_wait_seconds}s
                </span>
              </button>
              {expandedId === q.id && <QueueEntriesList api={api} queue={q} />}
            </li>
          ))}
        </ul>
      )}

      <form className="space-y-3 rounded-md border border-border p-3" onSubmit={submit}>
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="q-name">
              Name
            </label>
            <Input id="q-name" aria-label="Queue name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="q-ring-group">
              Ring group
            </label>
            <select
              id="q-ring-group"
              aria-label="Queue ring group"
              className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              value={ringGroupId}
              onChange={(e) => setRingGroupId(e.target.value)}
            >
              <option value="">None</option>
              {(ringGroups ?? []).map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="q-hold-audio">
            Hold audio URL
          </label>
          <Input
            id="q-hold-audio"
            aria-label="Hold audio URL"
            placeholder="https://…"
            value={holdAudioUrl}
            onChange={(e) => setHoldAudioUrl(e.target.value)}
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="q-max-wait">
              Max wait (seconds)
            </label>
            <Input
              id="q-max-wait"
              aria-label="Max wait seconds"
              type="number"
              min={10}
              max={3600}
              value={maxWaitSeconds}
              onChange={(e) => setMaxWaitSeconds(Number(e.target.value))}
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="q-overflow">
              Overflow
            </label>
            <select
              id="q-overflow"
              aria-label="Overflow action"
              className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              value={overflow}
              onChange={(e) => setOverflow(e.target.value)}
            >
              {OVERFLOW_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <Button type="submit" size="sm" disabled={!name.trim() || createQueue.isPending}>
          Create queue
        </Button>
      </form>
    </section>
  );
}

function queueEntryBadgeClass(state: string): string {
  switch (state) {
    case "waiting":
      return "bg-amber-100 text-amber-800";
    case "offered":
      return "bg-blue-100 text-blue-800";
    case "connected":
      return "bg-green-100 text-green-800";
    case "abandoned":
    case "overflowed":
      return "bg-red-100 text-red-800";
    case "callback_requested":
      return "bg-purple-100 text-purple-800";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

/** Polls only while this queue's card is expanded (visible) - the hook itself also pauses
 * while the tab is hidden. */
function QueueEntriesList({ api, queue }: { api: ApiClient; queue: QueueOut }) {
  const { data: entries, isLoading } = useQueueEntries(api, queue.id, { enabled: true });

  return (
    <div className="mt-2 border-t border-border pt-2">
      {isLoading ? (
        <Spinner label="Loading entries" />
      ) : (entries ?? []).length === 0 ? (
        <p className="text-xs text-muted-foreground">No entries.</p>
      ) : (
        <ul aria-label={`${queue.name} entries`} className="space-y-1">
          {(entries ?? []).map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-2 text-xs">
              <span>
                {e.state === "waiting" && e.position != null ? `#${e.position + 1}` : e.call_id.slice(0, 8)}
                {e.callback_e164 ? ` · ${e.callback_e164}` : ""}
              </span>
              <Badge className={cn(queueEntryBadgeClass(e.state))}>{e.state}</Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------------------------
 * Voicemails inbox (DR-8)
 * ------------------------------------------------------------------------------------- */
function VoicemailsSection({ api }: { api: ApiClient }) {
  const [statusFilter, setStatusFilter] = React.useState<string | undefined>("new");
  const { data: voicemails, isLoading } = useVoicemails(api, statusFilter);
  const markRead = useMarkVoicemailRead(api);
  const [error, setError] = React.useState<string | null>(null);

  async function markAsRead(id: string) {
    setError(null);
    try {
      await markRead.mutateAsync(id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Voicemails</h2>
        <select
          aria-label="Voicemail status filter"
          className="h-8 rounded-md border border-border bg-background px-2 text-xs"
          value={statusFilter ?? ""}
          onChange={(e) => setStatusFilter(e.target.value || undefined)}
        >
          <option value="new">New</option>
          <option value="read">Read</option>
          <option value="">All</option>
        </select>
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {isLoading ? (
        <Spinner label="Loading voicemails" />
      ) : (voicemails ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No voicemails.</p>
      ) : (
        <ul aria-label="Voicemails" className="divide-y divide-border rounded-md border border-border">
          {(voicemails ?? []).map((v) => (
            <li key={v.id} className="space-y-1 p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-muted-foreground">{relativeTime(v.created_at)} ago</span>
                <div className="flex items-center gap-2">
                  <Badge className="bg-gray-100 text-gray-600">{v.transcript_status}</Badge>
                  <Badge className={v.status === "new" ? "bg-amber-100 text-amber-800" : "bg-gray-100 text-gray-600"}>
                    {v.status}
                  </Badge>
                  {v.status === "new" && (
                    <Button type="button" size="sm" variant="outline" onClick={() => markAsRead(v.id)} disabled={markRead.isPending}>
                      Mark read
                    </Button>
                  )}
                </div>
              </div>
              {v.transcript ? (
                <p className="text-xs text-muted-foreground">{v.transcript}</p>
              ) : (
                <p className="text-xs italic text-muted-foreground">
                  {v.transcript_status === "disabled" ? "Transcription not configured." : "Transcript pending."}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
