import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useCancelOutboundCampaign,
  useCreateOutboundCampaign,
  useLists,
  useNumbers,
  useOutboundCampaign,
  useOutboundCampaignProgress,
  useOutboundCampaigns,
  usePauseOutboundCampaign,
  useStartOutboundCampaign,
  type CreateOutboundCampaignVars,
  type ListOut,
  type OutboundCampaignOut,
} from "@/api/hooks";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const DIALER_MODES = [
  { value: "preview", label: "Preview" },
  { value: "power", label: "Power" },
  { value: "parallel", label: "Parallel" },
  { value: "predictive", label: "Predictive" },
];

function campaignStatusBadgeClass(status: string): string {
  switch (status) {
    case "running":
      return "bg-green-100 text-green-800";
    case "paused":
    case "scheduled":
      return "bg-amber-100 text-amber-800";
    case "completed":
      return "bg-blue-100 text-blue-800";
    case "cancelled":
      return "bg-gray-100 text-gray-600";
    case "failed":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

export function CampaignsPage() {
  const { api } = useAuth();
  const { data: campaigns, isLoading, error } = useOutboundCampaigns(api);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [creating, setCreating] = React.useState(false);

  return (
    <div className="grid h-full grid-cols-[minmax(300px,380px)_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="flex items-center justify-between gap-2 border-b border-border p-3">
          <h1 className="text-lg font-semibold">Campaigns</h1>
          <Button
            type="button"
            size="sm"
            onClick={() => {
              setSelectedId(null);
              setCreating(true);
            }}
          >
            New
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading campaigns" />
          ) : error ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (campaigns ?? []).length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No campaigns yet.</p>
          ) : (
            <ul aria-label="Campaigns">
              {(campaigns ?? []).map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    aria-current={c.id === selectedId ? "true" : undefined}
                    onClick={() => {
                      setCreating(false);
                      setSelectedId(c.id);
                    }}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-muted",
                      c.id === selectedId && !creating && "bg-muted",
                    )}
                  >
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate font-medium">{c.name}</span>
                      <span className="text-xs text-muted-foreground">{c.channel}</span>
                    </span>
                    <Badge className={campaignStatusBadgeClass(c.status)}>{c.status}</Badge>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto">
        {creating ? (
          <CampaignForm
            api={api}
            onCreated={(created) => {
              setCreating(false);
              setSelectedId(created.id);
            }}
            onCancel={() => setCreating(false)}
          />
        ) : selectedId ? (
          <CampaignDetail api={api} campaignId={selectedId} />
        ) : (
          <p className="p-6 text-sm text-muted-foreground">
            Select a campaign or create a new one.
          </p>
        )}
      </section>
    </div>
  );
}

function CampaignForm({
  api,
  onCreated,
  onCancel,
}: {
  api: ApiClient;
  onCreated: (campaign: OutboundCampaignOut) => void;
  onCancel: () => void;
}) {
  const { data: lists } = useLists(api);
  const { data: numbers } = useNumbers(api);
  const createCampaign = useCreateOutboundCampaign(api);

  const [name, setName] = React.useState("");
  const [channel, setChannel] = React.useState<"sms" | "voice">("sms");
  const [listId, setListId] = React.useState("");
  const [body, setBody] = React.useState("");
  const [fromNumbers, setFromNumbers] = React.useState<string[]>([]);
  const [ratePerMinute, setRatePerMinute] = React.useState(6);
  const [dailyCap, setDailyCap] = React.useState(200);
  const [respectWarmup, setRespectWarmup] = React.useState(true);
  const [maxAttempts, setMaxAttempts] = React.useState(2);
  const [dialerMode, setDialerMode] = React.useState<string>("");
  const [parallelLines, setParallelLines] = React.useState(1);
  const [localPresence, setLocalPresence] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const activeNumbers = (numbers ?? []).filter((n) => n.is_active);

  function toggleNumber(e164: string) {
    setFromNumbers((prev) =>
      prev.includes(e164) ? prev.filter((n) => n !== e164) : [...prev, e164],
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const vars: CreateOutboundCampaignVars = {
      name: name.trim(),
      channel,
      list_id: listId,
    };
    if (channel === "sms") {
      vars.body = body || null;
      vars.from_numbers = fromNumbers;
      vars.rate_per_minute = ratePerMinute;
      vars.daily_cap = dailyCap;
      vars.respect_warmup = respectWarmup;
      vars.max_attempts = maxAttempts;
    } else {
      vars.dialer_mode = dialerMode;
      vars.parallel_lines = parallelLines;
      vars.local_presence = localPresence;
    }
    try {
      const created = await createCampaign.mutateAsync(vars);
      onCreated(created);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const canSubmit =
    name.trim().length > 0 && listId.length > 0 && (channel === "sms" || dialerMode.length > 0);

  return (
    <form className="max-w-xl space-y-4 p-6" onSubmit={submit}>
      <h2 className="text-base font-semibold">New campaign</h2>

      <div className="space-y-1">
        <label className="block text-xs text-muted-foreground" htmlFor="campaign-name">
          Name
        </label>
        <Input
          id="campaign-name"
          aria-label="Campaign name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
      </div>

      <div className="space-y-1">
        <label className="block text-xs text-muted-foreground" htmlFor="campaign-list">
          List
        </label>
        <select
          id="campaign-list"
          aria-label="Contact list"
          className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
          value={listId}
          onChange={(e) => setListId(e.target.value)}
          required
        >
          <option value="">Select a list…</option>
          {(lists ?? []).map((l: ListOut) => (
            <option key={l.id} value={l.id}>
              {l.name} ({l.status}, {l.accepted_count} accepted)
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-1">
        <span className="block text-xs text-muted-foreground">Channel</span>
        <div className="flex gap-1" role="radiogroup" aria-label="Channel">
          {(["sms", "voice"] as const).map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={channel === c}
              onClick={() => setChannel(c)}
              className={cn(
                "rounded-md border border-border px-3 py-1.5 text-sm",
                channel === c ? "bg-muted font-medium" : "hover:bg-muted",
              )}
            >
              {c === "sms" ? "SMS" : "Voice"}
            </button>
          ))}
        </div>
      </div>

      {channel === "sms" ? (
        <fieldset className="space-y-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-medium text-muted-foreground">SMS</legend>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="campaign-body">
              Message body
            </label>
            <textarea
              id="campaign-body"
              aria-label="Message body"
              rows={4}
              placeholder="Hi {{first_name}}, ..."
              className="flex w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
            <p className="text-[11px] text-muted-foreground">
              Use <code>{"{{merge}}"}</code> fields from the list (first_name, last_name,
              email, company). Leave empty to send each row&apos;s own message column
              instead — the campaign needs one or the other to start.
            </p>
          </div>

          <div className="space-y-1">
            <span className="block text-xs text-muted-foreground">From numbers</span>
            <div
              className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-border p-2"
              role="group"
              aria-label="From numbers"
            >
              {activeNumbers.length === 0 ? (
                <p className="text-xs text-muted-foreground">No active numbers.</p>
              ) : (
                activeNumbers.map((n) => (
                  <label key={n.id} className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={fromNumbers.includes(n.e164)}
                      onChange={() => toggleNumber(n.e164)}
                    />
                    {formatPhone(n.e164)}
                  </label>
                ))
              )}
            </div>
            <p className="text-[11px] text-muted-foreground">
              None selected = full active pool (rotates).
            </p>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="campaign-rate">
                Rate/min
              </label>
              <Input
                id="campaign-rate"
                aria-label="Rate per minute"
                type="number"
                min={1}
                max={600}
                value={ratePerMinute}
                onChange={(e) => setRatePerMinute(Number(e.target.value))}
              />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="campaign-cap">
                Daily cap
              </label>
              <Input
                id="campaign-cap"
                aria-label="Daily cap"
                type="number"
                min={1}
                value={dailyCap}
                onChange={(e) => setDailyCap(Number(e.target.value))}
              />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="campaign-attempts">
                Max attempts
              </label>
              <Input
                id="campaign-attempts"
                aria-label="Max attempts"
                type="number"
                min={1}
                max={10}
                value={maxAttempts}
                onChange={(e) => setMaxAttempts(Number(e.target.value))}
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={respectWarmup}
              onChange={(e) => setRespectWarmup(e.target.checked)}
            />
            Respect number warm-up ramp
          </label>
        </fieldset>
      ) : (
        <fieldset className="space-y-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-medium text-muted-foreground">Voice</legend>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="campaign-dialer-mode">
              Dialer mode
            </label>
            <select
              id="campaign-dialer-mode"
              aria-label="Dialer mode"
              className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              value={dialerMode}
              onChange={(e) => setDialerMode(e.target.value)}
              required
            >
              <option value="">Select a mode…</option>
              {DIALER_MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="campaign-parallel">
              Parallel lines
            </label>
            <Input
              id="campaign-parallel"
              aria-label="Parallel lines"
              type="number"
              min={1}
              max={20}
              value={parallelLines}
              onChange={(e) => setParallelLines(Number(e.target.value))}
              disabled={dialerMode !== "parallel"}
            />
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={localPresence}
              onChange={(e) => setLocalPresence(e.target.checked)}
            />
            Local presence (match caller ID area code to the contact)
          </label>
        </fieldset>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <Button type="submit" disabled={!canSubmit || createCampaign.isPending}>
          {createCampaign.isPending ? "Creating…" : "Create campaign"}
        </Button>
        <Button type="button" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function CampaignDetail({ api, campaignId }: { api: ApiClient; campaignId: string }) {
  const { data: campaign, isLoading } = useOutboundCampaign(api, campaignId);
  const { data: progress } = useOutboundCampaignProgress(api, campaignId);
  const startCampaign = useStartOutboundCampaign(api);
  const pauseCampaign = usePauseOutboundCampaign(api);
  const cancelCampaign = useCancelOutboundCampaign(api);
  const [actionError, setActionError] = React.useState<string | null>(null);

  if (isLoading || !campaign) return <Spinner label="Loading campaign" />;

  const canStart = ["draft", "scheduled", "paused"].includes(campaign.status);
  const canPause = campaign.status === "running";
  const canCancel = !["completed", "cancelled"].includes(campaign.status);

  async function run(action: "start" | "pause" | "cancel") {
    setActionError(null);
    try {
      if (action === "start") await startCampaign.mutateAsync(campaignId);
      if (action === "pause") await pauseCampaign.mutateAsync(campaignId);
      if (action === "cancel") await cancelCampaign.mutateAsync(campaignId);
    } catch (err) {
      setActionError((err as Error).message);
    }
  }

  const acting = startCampaign.isPending || pauseCampaign.isPending || cancelCampaign.isPending;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-base font-semibold">{campaign.name}</h2>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm text-muted-foreground">
          <dt>Channel</dt>
          <dd>{campaign.channel}</dd>
          <dt>Status</dt>
          <dd>
            <Badge className={campaignStatusBadgeClass(campaign.status)}>{campaign.status}</Badge>
          </dd>
          {campaign.channel === "sms" ? (
            <>
              <dt>Rate/min</dt>
              <dd>{campaign.rate_per_minute}</dd>
              <dt>Daily cap</dt>
              <dd>{campaign.daily_cap}</dd>
              <dt>Max attempts</dt>
              <dd>{campaign.max_attempts}</dd>
              <dt>Warm-up</dt>
              <dd>{campaign.respect_warmup ? "Respected" : "Ignored"}</dd>
            </>
          ) : (
            <>
              <dt>Dialer mode</dt>
              <dd>{campaign.dialer_mode ?? "—"}</dd>
              <dt>Parallel lines</dt>
              <dd>{campaign.parallel_lines}</dd>
              <dt>Local presence</dt>
              <dd>{campaign.local_presence ? "On" : "Off"}</dd>
            </>
          )}
        </dl>
      </div>

      {actionError && (
        <p role="alert" className="text-sm text-destructive">
          {actionError}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <Button type="button" onClick={() => run("start")} disabled={!canStart || acting}>
          Start
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => run("pause")}
          disabled={!canPause || acting}
        >
          Pause
        </Button>
        <Button
          type="button"
          variant="destructive"
          onClick={() => run("cancel")}
          disabled={!canCancel || acting}
        >
          Cancel
        </Button>
      </div>

      <div>
        <h3 className="text-sm font-medium">Progress</h3>
        {!progress ? (
          <Spinner label="Loading progress" />
        ) : (
          <div className="mt-2 space-y-2">
            <p className="text-xs text-muted-foreground">{progress.total} total</p>
            <ul className="flex flex-wrap gap-2" aria-label="Progress by status">
              {Object.entries(progress.counts).map(([key, count]) => (
                <li key={key}>
                  <Badge className="bg-muted text-foreground">
                    {key}: {count}
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
