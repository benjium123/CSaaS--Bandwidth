import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useAgentProfiles,
  useAssignCampaign,
  useCampaigns,
  type CampaignOut,
} from "@/api/hooks";
import { fetchInboxes, type Inbox } from "@/api/conversations";
import {
  answeredBy,
  useAvailableNumbers,
  useNumbers,
  useOrderNumber,
  useSetAnsweredBy,
  type NumberOut,
} from "@/api/numbers";
import { CONSOLE_PANEL as PANEL, InitialsAvatar } from "@/components/ui/consoleChrome";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  mutationErrorMessage,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import { PortingPanel } from "@/components/numbers/PortingPanel";
import { formatPhone } from "@/lib/format";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { cn } from "@/lib/utils";

/** 10DLC limit per campaign. The API sends `number_cap`; 50 is the documented fallback. */
export const DEFAULT_CAMPAIGN_CAP = 50;

export type CampaignWithCap = CampaignOut & { number_cap?: number | null };

export function campaignCap(c: CampaignWithCap): number {
  return c.number_cap ?? DEFAULT_CAMPAIGN_CAP;
}

export type LineStatusKey = "order_failed" | "order_pending" | "no_911" | "cant_text" | "texting_pending" | "ready";

export type LineStatus = { key: LineStatusKey; label: string; tone: PillTone; needsAttention: boolean };

/**
 * ONE status per line. Precedence (most important first): the order itself (failed, then
 * pending) > no 911 address > texting (rejected/not registered, then in review) > ready.
 */
export function lineStatus(n: NumberOut): LineStatus {
  if (n.status === "failed")
    return { key: "order_failed", label: "Order failed", tone: "danger", needsAttention: true };
  if (n.status === "pending")
    return { key: "order_pending", label: "Order pending", tone: "warning", needsAttention: true };
  if (
    n.e911_supported &&
    (n.emergency_status === "missing" || n.emergency_status === "failed")
  )
    return { key: "no_911", label: "No 911 address", tone: "danger", needsAttention: true };
  if (n.registration === "approved")
    return { key: "ready", label: "Ready", tone: "success", needsAttention: false };
  if (n.registration === "pending")
    return { key: "texting_pending", label: "Texting pending", tone: "warning", needsAttention: true };
  return { key: "cant_text", label: "Can't text", tone: "warning", needsAttention: true };
}

/* ── Shared controls (also used by LineDetailPage) ─────────────────────────────────── */

/**
 * Inline 10DLC campaign picker. Every option reads "Name (used/cap)"; a full campaign is
 * disabled unless it is the one this number is already on. A refused assignment (409
 * campaign_full) shows the server's message right under the control.
 */
export function CampaignPicker({
  number,
  campaigns,
}: {
  number: NumberOut;
  campaigns: CampaignWithCap[];
}) {
  const { api } = useAuth();
  const assign = useAssignCampaign(api);
  const current = campaigns.find((c) => c.id === number.campaign_id);

  if (number.number_type !== "local") {
    return <span className="text-xs text-muted-foreground">{current?.name ?? "—"}</span>;
  }
  return (
    <div className="space-y-1">
      <Select
        aria-label={`Campaign for ${number.e164}`}
        className="h-8 px-2 text-xs text-foreground"
        value={number.campaign_id ?? ""}
        disabled={assign.isPending}
        onChange={(e) =>
          assign.mutate({ numberId: number.id, campaign_id: e.target.value || null })
        }
      >
        {/* A carrier-side campaign can be moved but not cleared (the API refuses). */}
        <option value="" disabled={number.carrier_campaign_locked && Boolean(number.campaign_id)}>
          No campaign
        </option>
        {campaigns.map((c) => {
          const cap = campaignCap(c);
          const full = c.number_count >= cap;
          return (
            <option key={c.id} value={c.id} disabled={full && c.id !== number.campaign_id}>
              {`${c.name} (${c.number_count}/${cap})`}
              {full ? " - full" : ""}
            </option>
          );
        })}
      </Select>
      {assign.isError && (
        <p role="alert" className="text-xs text-destructive">
          {mutationErrorMessage(assign.error)}
        </p>
      )}
    </div>
  );
}

/** "Who answers" control: a person, or one of the workspace's assistants. */
export function AnsweredByControl({ number }: { number: NumberOut }) {
  const { api } = useAuth();
  const { data: assistants } = useAgentProfiles(api);
  const setAnsweredBy = useSetAnsweredBy(api);
  const current = answeredBy(number);
  const list = assistants ?? [];
  const assistantName =
    current.mode === "assistant"
      ? (list.find((a) => a.id === current.profile_id)?.name ?? null)
      : null;
  return (
    <div className="space-y-1">
      <Select
        aria-label={`Answered by for ${number.e164}`}
        className="h-8 px-2 text-xs text-foreground"
        value={current.mode === "assistant" ? (current.profile_id ?? "") : ""}
        disabled={setAnsweredBy.isPending || number.status === "released"}
        onChange={(e) =>
          e.target.value
            ? setAnsweredBy.mutate({ numberId: number.id, mode: "assistant", profile_id: e.target.value })
            : setAnsweredBy.mutate({ numberId: number.id, mode: "human", profile_id: null })
        }
      >
        <option value="">A person</option>
        {list.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
        {current.mode === "assistant" && current.profile_id && !assistantName && (
          <option value={current.profile_id}>An assistant that is no longer here</option>
        )}
      </Select>
      <MutationStatus pending={setAnsweredBy.isPending} error={setAnsweredBy.error} pendingLabel="Saving…" />
    </div>
  );
}

/* ── Lines list ─────────────────────────────────────────────────────────────────────── */

type Line = { number: NumberOut; inbox: Inbox | null };

const NO_DEPARTMENT = "__none__";

type Group = { id: string; name: string; lines: Line[] };

export function groupLines(lines: Line[]): Group[] {
  const byDept = new Map<string, Group>();
  const none: Group = { id: NO_DEPARTMENT, name: "No department", lines: [] };
  for (const line of lines) {
    const depts = [...(line.inbox?.departments ?? [])].sort((a, b) => a.name.localeCompare(b.name));
    if (depts.length === 0) {
      none.lines.push(line);
      continue;
    }
    // A line held by several departments is listed once, under the first by name.
    const first = depts[0];
    const group = byDept.get(first.id) ?? { id: first.id, name: first.name, lines: [] };
    group.lines.push(line);
    byDept.set(first.id, group);
  }
  const groups = [...byDept.values()].sort((a, b) => a.name.localeCompare(b.name));
  return none.lines.length > 0 ? [...groups, none] : groups;
}

function CampaignMeter({ campaigns }: { campaigns: CampaignWithCap[] }) {
  if (campaigns.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="10DLC campaigns">
      {campaigns.map((c) => {
        const cap = campaignCap(c);
        const left = cap - c.number_count;
        const tone: PillTone = left <= 0 ? "danger" : left <= 3 ? "warning" : "neutral";
        return (
          <Pill key={c.id} tone={tone}>
            {c.name} {c.number_count}/{cap}
            {left <= 0 ? " · Full · attach another campaign" : ""}
          </Pill>
        );
      })}
      <Link to="/settings/messaging" className="text-xs font-semibold text-primary underline">
        + Campaign
      </Link>
    </div>
  );
}

function LineRow({ line, campaigns }: { line: Line; campaigns: CampaignWithCap[] }) {
  const { number, inbox } = line;
  const status = lineStatus(number);
  const [, setSearchParams] = useSearchParams();
  const name = inbox?.name ?? null;
  const others = (inbox?.departments ?? []).length > 1 ? (inbox?.departments ?? []).slice(1) : [];
  const current = answeredBy(number);
  return (
    <li className={cn(PANEL, "flex flex-wrap items-center gap-x-4 gap-y-2 p-3")}>
      <button
        type="button"
        className="flex min-w-[14rem] flex-1 items-center gap-3 text-left"
        aria-label={`Open line ${formatPhone(number.e164)}`}
        onClick={() =>
          setSearchParams((prev) => {
            const next = new URLSearchParams(prev);
            next.set("line", number.id);
            next.delete("tab");
            return next;
          })
        }
      >
        <InitialsAvatar size="row" seed={number.id} name={name ?? formatPhone(number.e164)} />
        <span className="min-w-0">
          <span className="block whitespace-nowrap font-semibold text-foreground">
            {formatPhone(number.e164)}
          </span>
          <span className="block truncate text-xs text-muted-foreground">
            {name ?? "Not in an inbox"}
            {others.length > 0 ? ` · also ${others.map((d) => d.name).join(", ")}` : ""}
          </span>
        </span>
      </button>
      <Pill tone={status.tone}>{status.label}</Pill>
      <span className="w-24 text-xs text-muted-foreground">
        {current.mode === "assistant" ? "An assistant" : "A person"}
      </span>
      <div className="w-56">
        <CampaignPicker number={number} campaigns={campaigns} />
      </div>
    </li>
  );
}

function GetNumberSection({ api }: { api: ApiClient }) {
  const { me, orgId } = useAuth();
  const qc = useQueryClient();
  const paidCheckout =
    me?.memberships?.find((m) => m.org_id === orgId)?.number_subscription_required === true;
  const [open, setOpen] = React.useState(false);
  const [value, setValue] = React.useState("");
  const [addError, setAddError] = React.useState<string | null>(null);
  const [areaCode, setAreaCode] = React.useState("");
  const [numberType, setNumberType] = React.useState("local");
  const [filters, setFilters] = React.useState<{ area_code?: string; number_type: string } | null>(null);
  const available = useAvailableNumbers(api, filters ?? { number_type: "local" }, filters !== null);
  const order = useOrderNumber(api);
  const [ordered, setOrdered] = React.useState<string | null>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setAddError(null);
    try {
      await api.request("/api/v1/numbers", { method: "POST", json: { e164: value } });
      setValue("");
      qc.invalidateQueries({ queryKey: ["numbers"] });
    } catch (err) {
      setAddError((err as Error).message);
    }
  }

  return (
    <Section title="Get a number" description="Order a new number, add one you own, or port one in.">
      {!open ? (
        <Button type="button" className="rounded-full px-5" onClick={() => setOpen(true)}>
          + Get a number
        </Button>
      ) : paidCheckout ? (
        <Link to="/choose-numbers" className="font-semibold text-primary underline">
          Choose phone numbers
        </Link>
      ) : (
        <div className="space-y-3">
          <form className={cn(PANEL, "flex flex-wrap items-end gap-3 p-3.5")} onSubmit={(e) => {
            e.preventDefault();
            setOrdered(null);
            setFilters({ area_code: areaCode || undefined, number_type: numberType });
          }}>
            <Input aria-label="Area code" placeholder="214" className="w-24" value={areaCode}
              onChange={(e) => setAreaCode(e.target.value)} />
            <Select aria-label="Number type" value={numberType} onChange={(e) => setNumberType(e.target.value)}>
              <option value="local">Local</option>
              <option value="tollfree">Toll-free</option>
            </Select>
            <Button type="submit" className="rounded-full px-5" disabled={available.isFetching}>
              Search
            </Button>
            <MutationStatus pending={order.isPending} error={order.error} pendingLabel="Saving…" />
          </form>
          {ordered && <p className="text-sm text-foreground">Ordered {formatPhone(ordered)}.</p>}
          {available.isFetching ? (
            <Spinner label="Searching" />
          ) : available.isError ? (
            <p role="alert" className="text-sm text-destructive">{(available.error as Error).message}</p>
          ) : (available.data ?? []).length > 0 ? (
            <ul className="space-y-2">
              {(available.data ?? []).map((r) => (
                <li key={r.e164} className={cn(PANEL, "flex items-center justify-between gap-3 p-3")}>
                  <span className="font-semibold text-foreground">{formatPhone(r.e164)}</span>
                  <span className="flex-1 text-xs text-muted-foreground">
                    {[r.locality, r.region].filter(Boolean).join(", ")}
                  </span>
                  <Button type="button" size="sm" className="rounded-full px-4" disabled={order.isPending}
                    onClick={async () => {
                      try {
                        const o = await order.mutateAsync({ e164: r.e164 });
                        setOrdered(o.e164);
                      } catch {
                        /* shown by MutationStatus */
                      }
                    }}>
                    Order
                  </Button>
                </li>
              ))}
            </ul>
          ) : filters ? (
            <p className="text-sm text-muted-foreground">No numbers found. Try a different area code or type.</p>
          ) : null}
          <form className={cn(PANEL, "flex gap-3 p-3.5")} onSubmit={add}>
            <Input aria-label="Phone number" placeholder="+12145550100" value={value}
              onChange={(e) => setValue(e.target.value)} />
            <Button type="submit" className="rounded-full px-5">Add</Button>
          </form>
          {addError && <p role="alert" className="text-sm text-destructive">{addError}</p>}
        </div>
      )}
    </Section>
  );
}

export function LinesPage() {
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const numbersQuery = useNumbers(api);
  const inboxesQuery = useQuery({ queryKey: ["inboxes"], queryFn: () => fetchInboxes(api) });
  const campaignsQuery = useCampaigns(api);
  const [filter, setFilter] = React.useState<"all" | "attention">("all");

  const campaigns = (campaignsQuery.data ?? []) as CampaignWithCap[];
  const lines: Line[] = React.useMemo(() => {
    const inboxByNumber = new Map((inboxesQuery.data ?? []).map((i) => [i.number_id, i]));
    return (numbersQuery.data ?? [])
      .filter((n) => n.status !== "released")
      .map((n) => ({ number: n, inbox: inboxByNumber.get(n.id) ?? null }));
  }, [numbersQuery.data, inboxesQuery.data]);
  const shown = filter === "attention" ? lines.filter((l) => lineStatus(l.number).needsAttention) : lines;
  const groups = groupLines(shown);

  return (
    <div className={cn(surfaceThemeClass(theme), "mx-auto max-w-5xl space-y-8 bg-background p-6 text-foreground")}>
      <Section title="Lines" description="Every number in your workspace, with its inbox.">
        <div className="space-y-4">
          <CampaignMeter campaigns={campaigns} />
          <div className="flex gap-2" role="group" aria-label="Filter lines">
            <Button type="button" size="sm" className="rounded-full px-4"
              variant={filter === "all" ? "default" : "outline"} aria-pressed={filter === "all"}
              onClick={() => setFilter("all")}>All</Button>
            <Button type="button" size="sm" className="rounded-full px-4"
              variant={filter === "attention" ? "default" : "outline"} aria-pressed={filter === "attention"}
              onClick={() => setFilter("attention")}>Needs attention</Button>
          </div>
          {numbersQuery.isLoading ? (
            <Spinner />
          ) : numbersQuery.isError ? (
            <div role="alert" className="flex items-center gap-3 text-sm text-destructive">
              <span>{(numbersQuery.error as Error).message}</span>
              <Button type="button" size="sm" variant="outline" className="rounded-full px-3.5"
                onClick={() => numbersQuery.refetch()}>Retry</Button>
            </div>
          ) : groups.length === 0 ? (
            <EmptyState
              title={filter === "attention" ? "Nothing needs attention" : "No lines yet"}
              description={filter === "attention" ? "Every line is ready." : "Get a number below to create your first line."}
            />
          ) : (
            groups.map((g) => (
              <section key={g.id} aria-label={g.name} className="space-y-2">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.07em] text-[hsl(var(--cx-muted))]">
                  {g.name}
                </h3>
                <ul className="space-y-2">
                  {g.lines.map((l) => (
                    <LineRow key={l.number.id} line={l} campaigns={campaigns} />
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>
      </Section>
      <GetNumberSection api={api} />
      <PortingPanel api={api} numbers={numbersQuery.data ?? []} />
    </div>
  );
}
