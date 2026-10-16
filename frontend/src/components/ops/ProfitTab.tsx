import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Pill, Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import {
  CONSOLE_CELL,
  CONSOLE_CELL_L,
  CONSOLE_CELL_R,
  CONSOLE_HEAD,
  CONSOLE_ROW,
  CONSOLE_TABLE,
  ConsoleEmpty,
  FilterPill,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";
import { cn } from "@/lib/utils";

/** Switchboard > Profit (owner 2026-10-16): what we charged, what carriers, AI and Stripe
 * cost us and the profit, per day, for every workspace or one, for every service or one.
 * The server returns each day split by service, so the toggles filter here without a new
 * request. Also the SignalWire balance guard: the estimate, and where the owner enters the
 * balance after a top-up. */

type Money = {
  charged: number;
  carrier_cost: number;
  ai_cost: number;
  stripe_fees: number;
  profit: number;
  units?: number;
  fixed_costs?: number;
};
type ProfitDay = {
  date: string;
  services: Record<string, Money>;
  unallocated_stripe_fees: number;
  fixed_costs: number;
  total: Money;
};
export type ProfitDaily = {
  start: string;
  end: string;
  org_id: string | null;
  org_name: string | null;
  services: { key: string; label: string }[];
  days: ProfitDay[];
  notes: string[];
};
type OrgOption = { org_id: string; name: string };
export type SignalWireGuard = {
  anchor_balance_micros: number | null;
  anchor_at: string | null;
  spent_micros: number;
  estimate_micros: number | null;
  checked_at: string | null;
  paused: boolean;
  warn_sent_at: string | null;
  stop_sent_at: string | null;
  warn_micros: number;
  stop_micros: number;
};

const PROFIT_PATH = "/api/v1/ops/console/profit-daily";
const GUARD_PATH = "/api/v1/ops/console/signalwire-guard";
const ORGS_PATH = "/api/v1/ops/console/orgs";
const DEFAULT_DAYS = 30;
const MICROS = 1_000_000;
const EMPTY: Money = { charged: 0, carrier_cost: 0, ai_cost: 0, stripe_fees: 0, profit: 0, units: 0 };

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 3,
});

export function fmtMicros(micros: number | null | undefined): string {
  return micros == null ? "—" : money.format(micros / MICROS);
}

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** One day's numbers for the chosen service ("all" = the day total incl. server + top-up fees). */
export function dayFigures(day: ProfitDay, service: string): Money {
  if (service === "all") return { ...day.total, fixed_costs: day.fixed_costs };
  return { ...EMPTY, ...(day.services[service] ?? {}), fixed_costs: 0 };
}

function sum(rows: Money[]): Money {
  const out: Money = { ...EMPTY, fixed_costs: 0 };
  for (const r of rows) {
    out.charged += r.charged;
    out.carrier_cost += r.carrier_cost;
    out.ai_cost += r.ai_cost;
    out.stripe_fees += r.stripe_fees;
    out.profit += r.profit;
    out.units = (out.units ?? 0) + (r.units ?? 0);
    out.fixed_costs = (out.fixed_costs ?? 0) + (r.fixed_costs ?? 0);
  }
  return out;
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" }) {
  return (
    <div className="rounded-lg border border-border px-3.5 py-2.5">
      <div className="text-[11.5px] uppercase tracking-wide text-[hsl(var(--cx-subtle))]">{label}</div>
      <div
        className={cn(
          "mt-0.5 text-[17px] font-semibold tabular-nums",
          tone === "good" && "text-emerald-600",
          tone === "bad" && "text-red-600",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function ProfitBars({ days, service }: { days: ProfitDay[]; service: string }) {
  const values = days.map((d) => dayFigures(d, service).profit);
  const peak = Math.max(1, ...values.map((v) => Math.abs(v)));
  return (
    <div className="flex h-28 items-center gap-[2px]" aria-label="Profit per day">
      {days.map((d, i) => {
        const v = values[i];
        const h = Math.round((Math.abs(v) / peak) * 50);
        return (
          <div key={d.date} className="flex h-full flex-1 flex-col justify-center" title={`${d.date}: ${fmtMicros(v)}`}>
            <div className="flex h-1/2 items-end">
              {v > 0 && <div className="w-full rounded-t-sm bg-emerald-500" style={{ height: `${h * 2}%` }} />}
            </div>
            <div className="flex h-1/2 items-start border-t border-border">
              {v < 0 && <div className="w-full rounded-b-sm bg-red-500" style={{ height: `${h * 2}%` }} />}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SignalWirePanel() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [balance, setBalance] = React.useState("");
  const guard = useQuery({
    queryKey: ["ops", "console", "signalwire-guard"],
    queryFn: () => api.request<SignalWireGuard>(GUARD_PATH),
    refetchInterval: 60_000,
  });
  const save = useMutation({
    mutationFn: (micros: number) =>
      api.request<SignalWireGuard>(GUARD_PATH, { method: "POST", json: { balance_micros: micros } }),
    onSuccess: (data) => {
      queryClient.setQueryData(["ops", "console", "signalwire-guard"], data);
      setBalance("");
    },
  });
  const g = guard.data;
  const parsed = Number(balance);
  const valid = balance.trim() !== "" && Number.isFinite(parsed) && parsed >= 0;

  return (
    <SurfaceCard className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <SectionLabel>SignalWire balance guard</SectionLabel>
        {g && (g.paused ? <Pill tone="danger">Paused</Pill> : <Pill tone="success">Running</Pill>)}
      </div>
      {guard.isLoading ? (
        <Spinner />
      ) : guard.error ? (
        <p className="text-[13px] text-red-600">{mutationErrorMessage(guard.error)}</p>
      ) : g && g.anchor_balance_micros == null ? (
        <p className="text-[13px] text-[hsl(var(--cx-subtle))]">No balance entered yet. The guard starts once you enter the SignalWire balance.</p>
      ) : g ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Estimated now" value={fmtMicros(g.estimate_micros)} tone={g.paused ? "bad" : undefined} />
          <Stat label="Entered" value={fmtMicros(g.anchor_balance_micros)} />
          <Stat label="Charged since" value={fmtMicros(g.spent_micros)} />
          <Stat label="Email / stop at" value={`${fmtMicros(g.warn_micros)} / ${fmtMicros(g.stop_micros)}`} />
        </div>
      ) : null}
      {g?.anchor_at && (
        <p className="text-[12px] text-[hsl(var(--cx-subtle))]">
          Entered {new Date(g.anchor_at).toLocaleString()}
          {g.checked_at ? ` · checked ${new Date(g.checked_at).toLocaleString()}` : ""}
          {g.warn_sent_at ? ` · low-balance email sent ${new Date(g.warn_sent_at).toLocaleString()}` : ""}
        </p>
      )}
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) save.mutate(Math.round(parsed * MICROS));
        }}
      >
        <Input
          aria-label="SignalWire balance in dollars"
          inputMode="decimal"
          placeholder="Balance from the SignalWire dashboard, e.g. 25.00"
          value={balance}
          onChange={(e) => setBalance(e.target.value)}
          className="max-w-xs"
        />
        <Button type="submit" disabled={!valid || save.isPending}>
          {save.isPending ? "Saving…" : "Save balance"}
        </Button>
        {save.error && <span className="text-[12.5px] text-red-600">{mutationErrorMessage(save.error)}</span>}
      </form>
      <p className="text-[12px] text-[hsl(var(--cx-subtle))]">
        Saving a new balance after a top-up re-arms the email and lifts the pause when it is above the stop level.
      </p>
    </SurfaceCard>
  );
}

export function ProfitTab() {
  const { api } = useAuth();
  const today = React.useMemo(() => new Date(), []);
  const [start, setStart] = React.useState(() => isoDay(new Date(today.getTime() - (DEFAULT_DAYS - 1) * 86_400_000)));
  const [end, setEnd] = React.useState(() => isoDay(today));
  const [orgId, setOrgId] = React.useState("");
  const [service, setService] = React.useState("all");

  const orgs = useQuery({
    queryKey: ["ops", "console", "orgs-list"],
    queryFn: async () => (await api.request<{ orgs?: OrgOption[] }>(ORGS_PATH)).orgs ?? [],
  });
  const profit = useQuery({
    queryKey: ["ops", "console", "profit-daily", start, end, orgId],
    queryFn: () => {
      const q = new URLSearchParams({ start, end });
      if (orgId) q.set("org_id", orgId);
      return api.request<ProfitDaily>(`${PROFIT_PATH}?${q.toString()}`);
    },
  });

  const data = profit.data;
  const days = data?.days ?? [];
  const figures = days.map((d) => dayFigures(d, service));
  const total = sum(figures);
  const serviceLabel = service === "all" ? "All services" : data?.services.find((s) => s.key === service)?.label ?? service;

  return (
    <div className="space-y-4">
      <SignalWirePanel />

      <SurfaceCard className="space-y-3">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-[12px] text-[hsl(var(--cx-subtle))]">
            From
            <Input type="date" value={start} max={end} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="text-[12px] text-[hsl(var(--cx-subtle))]">
            To
            <Input type="date" value={end} min={start} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <label className="text-[12px] text-[hsl(var(--cx-subtle))]">
            Workspace
            <select
              aria-label="Workspace"
              className="block h-9 rounded-md border border-border bg-transparent px-2 text-[13px]"
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
            >
              <option value="">All workspaces</option>
              {(orgs.data ?? []).map((o) => (
                <option key={o.org_id} value={o.org_id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Service">
          <FilterPill active={service === "all"} onClick={() => setService("all")}>
            All
          </FilterPill>
          {(data?.services ?? []).map((s) => (
            <FilterPill key={s.key} active={service === s.key} onClick={() => setService(s.key)}>
              {s.label}
            </FilterPill>
          ))}
        </div>

        {profit.isLoading ? (
          <Spinner />
        ) : profit.error ? (
          <p className="text-[13px] text-red-600">{mutationErrorMessage(profit.error)}</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <Stat label="Charged" value={fmtMicros(total.charged)} />
              <Stat label="Carrier cost" value={fmtMicros(total.carrier_cost)} />
              <Stat label="AI cost" value={fmtMicros(total.ai_cost)} />
              <Stat label="Stripe fees" value={fmtMicros(total.stripe_fees)} />
              <Stat label="Server" value={fmtMicros(total.fixed_costs ?? 0)} />
              <Stat label="Profit" value={fmtMicros(total.profit)} tone={total.profit >= 0 ? "good" : "bad"} />
            </div>
            <p className="text-[12px] text-[hsl(var(--cx-subtle))]">
              {serviceLabel} · {data?.org_name ?? "all workspaces"} · {data?.start} to {data?.end}
              {service !== "all" ? " · server cost and top-up Stripe fees show under All only" : ""}
            </p>
            <ProfitBars days={days} service={service} />
            {figures.every((f) => f.charged === 0 && f.carrier_cost === 0 && f.ai_cost === 0 && f.stripe_fees === 0 && !f.fixed_costs) ? (
              <ConsoleEmpty>Nothing in this range.</ConsoleEmpty>
            ) : (
              <div className="overflow-x-auto">
                <table className={CONSOLE_TABLE}>
                  <thead>
                    <tr>
                      <th className={CONSOLE_HEAD}>Day</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Usage</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Charged</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Carrier</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>AI</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Stripe</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Server</th>
                      <th className={cn(CONSOLE_HEAD, "text-right")}>Profit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...days].reverse().map((d) => {
                      const f = dayFigures(d, service);
                      const units = service === "all" ? null : f.units ?? 0;
                      return (
                        <tr key={d.date} className={CONSOLE_ROW}>
                          <td className={CONSOLE_CELL_L}>{d.date}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{units == null ? "—" : units}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{fmtMicros(f.charged)}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{fmtMicros(f.carrier_cost)}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{fmtMicros(f.ai_cost)}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{fmtMicros(f.stripe_fees)}</td>
                          <td className={cn(CONSOLE_CELL, "text-right tabular-nums")}>{fmtMicros(f.fixed_costs ?? 0)}</td>
                          <td
                            className={cn(
                              CONSOLE_CELL_R,
                              "text-right font-semibold tabular-nums",
                              f.profit >= 0 ? "text-emerald-600" : "text-red-600",
                            )}
                          >
                            {fmtMicros(f.profit)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {(data?.notes ?? []).map((n) => (
              <p key={n} className="text-[12px] text-[hsl(var(--cx-subtle))]">
                {n}
              </p>
            ))}
          </>
        )}
      </SurfaceCard>
    </div>
  );
}
