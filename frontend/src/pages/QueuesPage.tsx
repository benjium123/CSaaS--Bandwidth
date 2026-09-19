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
import {
  Badge,
  Button,
  EmptyState,
  Input,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import {
  ConsoleCard,
  InitialsAvatar,
  PageHeader,
  SurfaceCard,
} from "@/components/ui/consoleChrome";
import { relativeTime } from "@/lib/format";

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
    <div className="mx-auto max-w-4xl space-y-[18px] overflow-y-auto">
      <PageHeader
        title={<>Queues &amp; routing</>}
        description="When you are open, who rings, and what happens to the people waiting."
      />
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
  const { data: hours, isLoading, error: hoursError, refetch: refetchHours } = useBusinessHours(api);
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
      setName("default");
      setTimezone("America/Chicago");
      setSchedule({});
      setHolidayInput("");
      setHolidays([]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <Section title="Business hours">
      {isLoading ? (
        <Spinner label="Loading business hours" />
      ) : hoursError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
            {(hoursError as Error).message}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchHours()}>
            Retry
          </Button>
        </div>
      ) : (hours ?? []).length === 0 ? (
        <EmptyState title="No business hours configured yet." />
      ) : (
        <SurfaceCard className="overflow-hidden p-0">
          <ul aria-label="Business hours" className="divide-y divide-[hsl(var(--cx-line))]">
            {(hours ?? []).map((h) => (
              <li key={h.id} className="flex items-center gap-[11px] p-[12px] text-[13.5px]">
                <InitialsAvatar name={h.name} seed={h.id} size="sm" />
                <span className="font-semibold">{h.name}</span>
                <span className="ml-auto text-[11.5px] text-[hsl(var(--cx-muted))]">
                  {h.timezone} · {h.holidays.length} holiday{h.holidays.length === 1 ? "" : "s"}
                </span>
              </li>
            ))}
          </ul>
        </SurfaceCard>
      )}

      <ConsoleCard className="p-[14px]">
        <form className="space-y-3" onSubmit={submit}>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="bh-name">
                Name
              </label>
              <Input id="bh-name" aria-label="Business hours name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="bh-tz">
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
                <span className="w-10 text-xs text-[hsl(var(--cx-muted))]">{day.label}</span>
                {windowsFor(day.key).map((w, i) => (
                  <div key={i} className="flex items-center gap-1">
                    <Input
                      aria-label={`${day.label} window ${i + 1} open`}
                      className="h-8 w-24"
                      value={w[0]}
                      onChange={(e) => updateWindow(day.key, i, 0, e.target.value)}
                    />
                    <span className="text-xs text-[hsl(var(--cx-muted))]">–</span>
                    <Input
                      aria-label={`${day.label} window ${i + 1} close`}
                      className="h-8 w-24"
                      value={w[1]}
                      onChange={(e) => updateWindow(day.key, i, 1, e.target.value)}
                    />
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Remove ${day.label} window ${i + 1}`}
                      onClick={() => removeWindow(day.key, i)}
                    >
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
            <span className="block text-xs text-[hsl(var(--cx-muted))]">Holidays (ISO dates)</span>
            <div className="flex flex-wrap gap-1">
              {holidays.map((d) => (
                <Badge key={d} className="rounded-full bg-[hsl(var(--cx-overlay))] text-[hsl(var(--cx-text))]">
                  {d}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={`Remove holiday ${d}`}
                    className="ml-1 h-4 w-4 p-0 text-foreground hover:text-[hsl(var(--cx-danger))]"
                    onClick={() => setHolidays((prev) => prev.filter((x) => x !== d))}
                  >
                    ×
                  </Button>
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
            <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
              {error}
            </p>
          )}

          <Button type="submit" size="sm" disabled={createHours.isPending}>
            Save business hours
          </Button>
        </form>
      </ConsoleCard>
    </Section>
  );
}

/* ---------------------------------------------------------------------------------------
 * Ring groups (DR-5)
 * ------------------------------------------------------------------------------------- */
function RingGroupsSection({ api }: { api: ApiClient }) {
  const { data: groups, isLoading, error: groupsError, refetch: refetchGroups } = useRingGroups(api);
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
    <Section title="Ring groups">
      {isLoading ? (
        <Spinner label="Loading ring groups" />
      ) : groupsError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
            {(groupsError as Error).message}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchGroups()}>
            Retry
          </Button>
        </div>
      ) : (groups ?? []).length === 0 ? (
        <EmptyState title="No ring groups yet." />
      ) : (
        <SurfaceCard className="overflow-hidden p-0">
          <ul aria-label="Ring groups" className="divide-y divide-[hsl(var(--cx-line))]">
            {(groups ?? []).map((g) => (
              <li key={g.id} className="flex items-center gap-[11px] p-[12px] text-[13.5px]">
                <InitialsAvatar name={g.name} seed={g.id} size="sm" />
                <span className="font-semibold">{g.name}</span>
                <span className="ml-auto text-[11.5px] text-[hsl(var(--cx-muted))]">
                  {g.strategy} · {g.member_user_ids.length} member{g.member_user_ids.length === 1 ? "" : "s"} ·{" "}
                  {g.ring_timeout_seconds}s
                </span>
              </li>
            ))}
          </ul>
        </SurfaceCard>
      )}

      <ConsoleCard className="p-[14px]">
        <form className="space-y-3" onSubmit={submit}>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="rg-name">
                Name
              </label>
              <Input id="rg-name" aria-label="Ring group name" value={name} onChange={(e) => setName(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="rg-strategy">
                Strategy
              </label>
              <Select
                id="rg-strategy"
                aria-label="Ring strategy"
                className="h-9 w-full px-2 text-sm"
                value={strategy}
                onChange={(e) => setStrategy(e.target.value as "simultaneous" | "sequential")}
              >
                <option value="simultaneous">Simultaneous</option>
                <option value="sequential">Sequential</option>
              </Select>
            </div>
          </div>

          <div className="space-y-1">
            <span className="block text-xs text-[hsl(var(--cx-muted))]">Members</span>
            <div
              className="max-h-32 space-y-1 overflow-y-auto rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[9px]"
              role="group"
              aria-label="Members"
            >
              {(members ?? []).length === 0 ? (
                <EmptyState title="No team members." />
              ) : (
                (members ?? []).map((m) => (
                  <div
                    key={m.user_id}
                    className="flex items-center gap-[9px] rounded-[10px] px-[6px] py-[5px]"
                  >
                    <InitialsAvatar name={m.full_name} seed={m.user_id} size="sm" />
                    <label className="flex min-w-0 flex-1 items-center gap-[9px] text-[12.5px]">
                      <input
                        type="checkbox"
                        checked={memberIds.includes(m.user_id)}
                        onChange={() => toggleMember(m.user_id)}
                      />
                      {m.full_name} ({m.email})
                    </label>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="rg-timeout">
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
            <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
              {error}
            </p>
          )}

          <Button type="submit" size="sm" disabled={!name.trim() || createGroup.isPending}>
            Create ring group
          </Button>
        </form>
      </ConsoleCard>
    </Section>
  );
}

/* ---------------------------------------------------------------------------------------
 * Queues (DR-6): CRUD + a live-ish entries list for whichever queue is expanded.
 * ------------------------------------------------------------------------------------- */
function QueuesSection({ api }: { api: ApiClient }) {
  const { data: queues, isLoading, error: queuesError, refetch: refetchQueues } = useQueues(api);
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
    <Section title="Queues">
      {isLoading ? (
        <Spinner label="Loading queues" />
      ) : queuesError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
            {(queuesError as Error).message}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchQueues()}>
            Retry
          </Button>
        </div>
      ) : (queues ?? []).length === 0 ? (
        <EmptyState title="No queues yet." />
      ) : (
        <SurfaceCard className="overflow-hidden p-0">
          <ul aria-label="Queues" className="divide-y divide-[hsl(var(--cx-line))]">
            {(queues ?? []).map((q) => (
              <li key={q.id} className="p-[12px] text-[13.5px]">
                <Button
                  type="button"
                  variant="ghost"
                  className="h-auto w-full justify-between gap-2 px-0 py-0 text-left"
                  onClick={() => setExpandedId((prev) => (prev === q.id ? null : q.id))}
                >
                  <span className="flex items-center gap-[11px]">
                    <InitialsAvatar name={q.name} seed={q.id} size="sm" />
                    <span className="font-semibold">{q.name}</span>
                  </span>
                  <span className="text-[11.5px] font-normal text-[hsl(var(--cx-muted))]">
                    overflow: {q.overflow} · max wait {q.max_wait_seconds}s
                  </span>
                </Button>
                {expandedId === q.id && <QueueEntriesList api={api} queue={q} />}
              </li>
            ))}
          </ul>
        </SurfaceCard>
      )}

      <ConsoleCard className="p-[14px]">
        <form className="space-y-3" onSubmit={submit}>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="q-name">
                Name
              </label>
              <Input id="q-name" aria-label="Queue name" value={name} onChange={(e) => setName(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="q-ring-group">
                Ring group
              </label>
              <Select
                id="q-ring-group"
                aria-label="Queue ring group"
                className="h-9 w-full px-2 text-sm"
                value={ringGroupId}
                onChange={(e) => setRingGroupId(e.target.value)}
              >
                <option value="">None</option>
                {(ringGroups ?? []).map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="q-hold-audio">
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
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="q-max-wait">
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
              <label className="block text-xs text-[hsl(var(--cx-muted))]" htmlFor="q-overflow">
                Overflow
              </label>
              <Select
                id="q-overflow"
                aria-label="Overflow action"
                className="h-9 w-full px-2 text-sm"
                value={overflow}
                onChange={(e) => setOverflow(e.target.value)}
              >
                {OVERFLOW_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          {error && (
            <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
              {error}
            </p>
          )}

          <Button type="submit" size="sm" disabled={!name.trim() || createQueue.isPending}>
            Create queue
          </Button>
        </form>
      </ConsoleCard>
    </Section>
  );
}

function queueEntryTone(state: string): PillTone {
  switch (state) {
    case "waiting":
      return "warning";
    case "offered":
      return "info";
    case "connected":
      return "success";
    case "abandoned":
    case "overflowed":
      return "danger";
    case "callback_requested":
      return "info";
    default:
      return "neutral";
  }
}

/** Polls only while this queue's card is expanded (visible) - the hook itself also pauses
 * while the tab is hidden. */
function QueueEntriesList({ api, queue }: { api: ApiClient; queue: QueueOut }) {
  const {
    data: entries,
    isLoading,
    error: entriesError,
    refetch: refetchEntries,
  } = useQueueEntries(api, queue.id, { enabled: true });

  return (
    <div className="mt-[11px] border-t border-[hsl(var(--cx-line))] pt-[11px]">
      {isLoading ? (
        <Spinner label="Loading entries" />
      ) : entriesError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
            {(entriesError as Error).message}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchEntries()}>
            Retry
          </Button>
        </div>
      ) : (entries ?? []).length === 0 ? (
        <EmptyState title="No entries." />
      ) : (
        <ul aria-label={`${queue.name} entries`} className="space-y-1">
          {(entries ?? []).map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-2 rounded-[12px] bg-[hsl(var(--cx-overlay))] px-[10px] py-[7px] text-[12px]">
              <span>
                {e.state === "waiting" && e.position != null ? `#${e.position + 1}` : e.call_id.slice(0, 8)}
                {e.callback_e164 ? ` · ${e.callback_e164}` : ""}
              </span>
              <Pill tone={queueEntryTone(e.state)}>{e.state}</Pill>
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
  const {
    data: voicemails,
    isLoading,
    error: voicemailsError,
    refetch: refetchVoicemails,
  } = useVoicemails(api, statusFilter);
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
    <Section
      title="Voicemails"
      actions={
        <Select
          aria-label="Voicemail status filter"
          className="h-8 px-2 text-xs"
          value={statusFilter ?? ""}
          onChange={(e) => setStatusFilter(e.target.value || undefined)}
        >
          <option value="new">New</option>
          <option value="read">Read</option>
          <option value="">All</option>
        </Select>
      }
    >
      {error && (
        <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
          {error}
        </p>
      )}

      {isLoading ? (
        <Spinner label="Loading voicemails" />
      ) : voicemailsError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-[hsl(var(--cx-danger))]">
            {(voicemailsError as Error).message}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchVoicemails()}>
            Retry
          </Button>
        </div>
      ) : (voicemails ?? []).length === 0 ? (
        <EmptyState title="No voicemails." />
      ) : (
        <SurfaceCard className="overflow-hidden p-0">
          <ul aria-label="Voicemails" className="divide-y divide-[hsl(var(--cx-line))]">
            {(voicemails ?? []).map((v) => (
              <li key={v.id} className="space-y-[7px] p-[12px] text-[13.5px]">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11.5px] text-[hsl(var(--cx-muted))]">{relativeTime(v.created_at)}</span>
                  <div className="flex items-center gap-2">
                    <Pill tone="neutral">{v.transcript_status}</Pill>
                    <Pill tone={v.status === "new" ? "warning" : "neutral"}>{v.status}</Pill>
                    {v.status === "new" && (
                      <Button type="button" size="sm" variant="outline" onClick={() => markAsRead(v.id)} disabled={markRead.isPending}>
                        Mark read
                      </Button>
                    )}
                  </div>
                </div>
                {v.transcript ? (
                  <p className="text-[12.5px] text-[hsl(var(--cx-subtle))]">{v.transcript}</p>
                ) : (
                  <p className="text-[12.5px] italic text-[hsl(var(--cx-muted))]">
                    {v.transcript_status === "disabled" ? "Transcription not configured." : "Transcript pending."}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </SurfaceCard>
      )}
    </Section>
  );
}
