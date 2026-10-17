import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { fmtMicros, type ProfitDaily, type SignalWireGuard } from "@/components/ops/ProfitTab";
import { Spinner } from "@/components/ui/primitives";

/** Switchboard > Dashboard: the landing tab. The open work first, then the money - today's
 * revenue, carrier + AI cost and profit, both prepaid balances and, only for operators who may
 * see profit, the last 14 days as one line chart plus where each customer's money goes this
 * month. Read only: a card or a "needs action" row just moves the page. */

const PROFIT_PATH = "/api/v1/ops/console/profit-daily";
const ORGS_PATH = "/api/v1/ops/console/orgs";
const TELNYX_PATH = "/api/v1/ops/console/telnyx";
const GUARD_PATH = "/api/v1/ops/console/signalwire-guard";
const SUPPORT_PATH = "/api/v1/ops/console/support?status=open";
const UNREAD_PATH = "/api/v1/ops/site/chats/unread";

type Day = ProfitDaily["days"][number];
type OrgSummary = { org_id: string; name: string; metrics: Record<string, number> };
type Tone = "warn" | "bad";
type MoneyCard = { key: string; section: string; label: string; value: string; sub: string; tone?: Tone };

const MUTED = "text-sm text-[hsl(var(--cx-muted))]";
const CARD_GRID = "grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(170px,1fr))]";

function isoDay(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** "2026-10-04" -> local midnight. Never `new Date(iso)`: that parses as UTC and shifts the day. */
function localDay(iso: string): Date {
  const parts = iso.split("-").map(Number);
  return new Date(parts[0] ?? 1970, (parts[1] ?? 1) - 1, parts[2] ?? 1);
}

function shortDay(iso: string): string {
  return localDay(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** "▲ 24% vs yesterday"; the percentage disappears when there is no yesterday to compare to. */
function trendSub(current: number, previous: number): string {
  const arrow = current - previous >= 0 ? "▲" : "▼";
  if (!previous) return `${arrow} vs yesterday`;
  const pct = Math.round(((current - previous) / Math.abs(previous)) * 100);
  return `${arrow} ${Math.abs(pct)}% vs yesterday`;
}

function niceCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
  return Math.ceil(value / magnitude) * magnitude;
}

function KpiCard({
  label,
  value,
  sub,
  tone,
  onClick,
}: {
  label: string;
  value: string;
  sub: string;
  tone?: Tone;
  onClick: () => void;
}) {
  return (
    <button type="button" className="sb-kpi" data-tone={tone} onClick={onClick}>
      <span className="sb-kpi-label">{label}</span>
      <span className="sb-kpi-value">{value}</span>
      <span className="sb-kpi-sub">{sub}</span>
    </button>
  );
}

function RevenueChart({ days }: { days: Day[] }) {
  const rows = days.map((d) => ({
    date: d.date,
    charged: d.total.charged,
    cost: d.total.carrier_cost + d.total.ai_cost,
    profit: d.total.profit,
  }));

  if (rows.length === 0) {
    return <p className={MUTED}>No revenue in the last 14 days yet.</p>;
  }

  const W = 600;
  const H = 200;
  const PAD_L = 62;
  const PAD_R = 10;
  const PAD_T = 12;
  const PAD_B = 30;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  const values: number[] = [];
  for (const row of rows) values.push(row.charged, row.cost, row.profit);
  const lowest = Math.min(0, ...values);
  const highest = Math.max(0, ...values);
  const top = niceCeil(highest) || 1_000_000;
  const bottom = lowest < 0 ? -niceCeil(-lowest) : 0;
  const span = top - bottom || 1;

  const xAt = (i: number) => PAD_L + (rows.length === 1 ? plotW / 2 : (i / (rows.length - 1)) * plotW);
  const yAt = (v: number) => PAD_T + (1 - (v - bottom) / span) * plotH;

  const series = [
    { key: "charged", label: "Revenue", color: "var(--sb-sky)", values: rows.map((r) => r.charged) },
    { key: "cost", label: "Carrier + AI", color: "var(--sb-amber)", values: rows.map((r) => r.cost) },
    { key: "profit", label: "Profit", color: "var(--sb-go)", values: rows.map((r) => r.profit) },
  ];
  const gridValues = [bottom, (bottom + top) / 2, top];
  const labelIndices = rows.map((_, i) => i).filter((i) => i % 3 === 0 || i === rows.length - 1);

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto"
        role="img"
        aria-label="Revenue, carrier cost and profit per day, last 14 days"
      >
        {gridValues.map((v) => (
          <g key={`grid-${v}`}>
            <line x1={PAD_L} y1={yAt(v)} x2={W - PAD_R} y2={yAt(v)} stroke="var(--sb-line)" strokeWidth={1} />
            <text x={PAD_L - 6} y={yAt(v) + 3} textAnchor="end" fontSize={9} fill="var(--sb-mute)">
              {fmtMicros(v)}
            </text>
          </g>
        ))}
        {series.map((line) => (
          <g key={line.key}>
            <path
              d={line.values.map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ")}
              fill="none"
              stroke={line.color}
              strokeWidth={2}
            />
            {line.values.map((v, i) => (
              <circle key={`${line.key}-${rows[i]?.date ?? i}`} cx={xAt(i)} cy={yAt(v)} r={2} fill={line.color}>
                <title>{`${rows[i]?.date ?? ""}: ${line.label} ${fmtMicros(v)}`}</title>
              </circle>
            ))}
          </g>
        ))}
        {labelIndices.map((i) => (
          <text
            key={`x-${i}`}
            x={xAt(i)}
            y={H - PAD_B + 14}
            textAnchor="middle"
            fontSize={9}
            fill="var(--sb-mute)"
          >
            {shortDay(rows[i]?.date ?? "")}
          </text>
        ))}
      </svg>
      <div className="mt-2 flex flex-wrap gap-3 text-[12px] text-[hsl(var(--cx-muted))]">
        {series.map((line) => (
          <span key={line.key} className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: line.color }} />
            {line.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function CustomerChart({ orgs }: { orgs: OrgSummary[] }) {
  const totalProvider = orgs.reduce((acc, o) => acc + (o.metrics.provider_cost ?? 0), 0);
  const rows = orgs
    .map((o) => ({
      id: o.org_id,
      name: o.name,
      revenue: o.metrics.accrual_revenue ?? 0,
      provider: o.metrics.provider_cost ?? 0,
      stripe: o.metrics.stripe_fees ?? 0,
      profit: Math.max(0, o.metrics.accrual_profit ?? 0),
    }))
    .filter((row) => row.revenue > 0)
    .sort((a, b) => b.revenue - a.revenue)
    .slice(0, 10);
  const peak = rows.length > 0 ? rows[0]?.revenue ?? 0 : 0;
  const width = (value: number) => (peak > 0 ? (value / peak) * 100 : 0);

  if (rows.length === 0) {
    return <p className={MUTED}>No customer revenue this month yet.</p>;
  }

  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div
          key={row.id}
          className="grid items-center gap-2"
          style={{ gridTemplateColumns: "minmax(90px,160px) minmax(0,1fr) auto" }}
        >
          <span className="truncate text-[13px]" title={row.name}>
            {row.name}
          </span>
          <div className="h-3.5 overflow-hidden rounded bg-[var(--sb-lift)]">
            <div className="flex h-full">
              <span className="h-full" style={{ width: `${width(row.provider)}%`, background: "var(--sb-amber)" }} />
              <span className="h-full" style={{ width: `${width(row.stripe)}%`, background: "var(--sb-mute)" }} />
              <span className="h-full" style={{ width: `${width(row.profit)}%`, background: "var(--sb-go)" }} />
            </div>
          </div>
          <div className="text-right">
            <div className="text-[13px] tabular-nums">{fmtMicros(row.revenue)}</div>
            <div className="text-[11px] text-[hsl(var(--cx-muted))]">
              {totalProvider > 0
                ? `${((row.provider / totalProvider) * 100).toFixed(1)}% of carrier spend`
                : "— of carrier spend"}
            </div>
          </div>
        </div>
      ))}
      <div className="flex flex-wrap gap-3 pt-1 text-[12px] text-[hsl(var(--cx-muted))]">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: "var(--sb-amber)" }} />
          Paid to carriers + AI
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: "var(--sb-mute)" }} />
          Stripe fees
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: "var(--sb-go)" }} />
          Our profit
        </span>
        <span>Bar length = what we charged</span>
      </div>
    </div>
  );
}

export function DashboardTab({
  counts,
  sections,
  onNavigate,
}: {
  /** open-item counts already loaded by the page: keys queue, alerts, purchases, texting */
  counts: Record<string, number>;
  /** section ids this operator may see */
  sections: string[];
  /** called with a section id when a card or row is clicked */
  onNavigate: (section: string) => void;
}) {
  const { api } = useAuth();
  const money = sections.includes("profit");
  const hasSupport = sections.includes("support");
  const hasWebsite = sections.includes("website");

  const { today, start14, monthStart } = React.useMemo(() => {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 13);
    const first = new Date(now.getFullYear(), now.getMonth(), 1);
    return { today: isoDay(now), start14: isoDay(start), monthStart: isoDay(first) };
  }, []);

  const profit = useQuery({
    queryKey: ["ops", "dashboard", "profit-daily", start14, today],
    queryFn: () => api.request<ProfitDaily>(`${PROFIT_PATH}?start=${start14}&end=${today}`),
    enabled: money,
  });
  const orgs = useQuery({
    queryKey: ["ops", "dashboard", "orgs", monthStart, today],
    queryFn: () => api.request<{ orgs?: OrgSummary[] }>(`${ORGS_PATH}?start=${monthStart}&end=${today}`),
    enabled: money,
  });
  const telnyx = useQuery({
    queryKey: ["ops", "dashboard", "telnyx"],
    queryFn: () => api.request<{ balance: { balance?: string; currency?: string } | null }>(TELNYX_PATH),
    enabled: money,
  });
  const guard = useQuery({
    queryKey: ["ops", "dashboard", "signalwire-guard"],
    queryFn: () => api.request<SignalWireGuard>(GUARD_PATH),
    enabled: money,
  });
  const support = useQuery({
    queryKey: ["ops", "dashboard", "support-open"],
    queryFn: () => api.request<{ requests: unknown[] }>(SUPPORT_PATH),
    enabled: hasSupport,
  });
  const unread = useQuery({
    queryKey: ["ops", "dashboard", "unread-chats"],
    queryFn: () => api.request<{ count: number }>(UNREAD_PATH),
    enabled: hasWebsite,
  });

  const days = profit.data?.days ?? [];
  const lastDay = days.length > 0 ? days[days.length - 1] ?? null : null;
  const prevDay = days.length > 1 ? days[days.length - 2] ?? null : null;

  const moneyCards: MoneyCard[] = [];
  if (lastDay) {
    const total = lastDay.total;
    const before = prevDay?.total ?? null;
    const carrierAI = total.carrier_cost + total.ai_cost;
    moneyCards.push(
      {
        key: "revenue",
        section: "profit",
        label: "Revenue today",
        value: fmtMicros(total.charged),
        sub: trendSub(total.charged, before?.charged ?? 0),
      },
      {
        key: "cost",
        section: "profit",
        label: "Carrier + AI today",
        value: fmtMicros(carrierAI),
        sub: total.charged > 0 ? `${((carrierAI / total.charged) * 100).toFixed(1)}% of revenue` : "— of revenue",
      },
      {
        key: "profit",
        section: "profit",
        label: "Profit today",
        value: fmtMicros(total.profit),
        sub: trendSub(total.profit, before?.profit ?? 0),
        tone: total.profit < 0 ? "bad" : undefined,
      },
    );
  }

  const telnyxBalance = telnyx.data?.balance ?? null;
  moneyCards.push({
    key: "telnyx",
    section: "console",
    label: "Telnyx balance",
    value: telnyxBalance?.balance ? `$${telnyxBalance.balance}` : "—",
    sub: "prepaid float",
  });

  const wire = guard.data;
  if (wire) {
    const missingAnchor = wire.anchor_balance_micros == null;
    const estimate = wire.estimate_micros;
    moneyCards.push({
      key: "signalwire",
      section: "profit",
      label: "SignalWire balance",
      value: missingAnchor ? "—" : fmtMicros(estimate),
      sub: missingAnchor ? "enter the balance on Profit" : `pauses at ${fmtMicros(wire.stop_micros)}`,
      tone: wire.paused || (estimate != null && estimate <= wire.warn_micros) ? "bad" : undefined,
    });
  }

  const supportOpen = hasSupport ? support.data?.requests?.length ?? 0 : 0;
  const unreadCount = hasWebsite ? unread.data?.count ?? 0 : 0;

  const countCards = [
    {
      section: "queue",
      label: "Review queue",
      value: counts.queue ?? 0,
      bad: false,
      action: (n: number) => `${n} waiting in the review queue`,
    },
    {
      section: "alerts",
      label: "Security alerts",
      value: counts.alerts ?? 0,
      bad: true,
      action: (n: number) => (n === 1 ? "1 security alert" : `${n} security alerts`),
    },
    {
      section: "texting",
      label: "10DLC needing action",
      value: counts.texting ?? 0,
      bad: false,
      action: (n: number) =>
        n === 1 ? "1 texting registration needs attention" : `${n} texting registrations need attention`,
    },
    {
      section: "purchases",
      label: "Stuck number orders",
      value: counts.purchases ?? 0,
      bad: true,
      action: (n: number) => (n === 1 ? "1 number order stuck" : `${n} number orders stuck`),
    },
    {
      section: "support",
      label: "Open support requests",
      value: supportOpen,
      bad: false,
      action: (n: number) => (n === 1 ? "1 open support request" : `${n} open support requests`),
    },
    {
      section: "website",
      label: "Unread website chats",
      value: unreadCount,
      bad: false,
      action: (n: number) => (n === 1 ? "1 unread website chat" : `${n} unread website chats`),
    },
  ].filter((card) => sections.includes(card.section));

  const waiting = countCards.filter((card) => card.value > 0);

  const problems: string[] = [];
  if (money && profit.isError) problems.push("Could not load the daily profit numbers.");
  if (money && orgs.isError) problems.push("Could not load the customer breakdown.");
  if (money && telnyx.isError) problems.push("Could not load the Telnyx balance.");
  if (money && guard.isError) problems.push("Could not load the SignalWire balance.");
  if (hasSupport && support.isError) problems.push("Could not load support requests.");
  if (hasWebsite && unread.isError) problems.push("Could not load unread website chats.");

  return (
    <div className="space-y-6">
      {money && profit.isLoading ? <Spinner label="Loading the numbers" /> : null}

      <div className={CARD_GRID}>
        {money && !profit.isLoading
          ? moneyCards.map((card) => (
              <KpiCard
                key={card.key}
                label={card.label}
                value={card.value}
                sub={card.sub}
                tone={card.tone}
                onClick={() => onNavigate(card.section)}
              />
            ))
          : null}
        {countCards.map((card) => (
          <KpiCard
            key={card.section}
            label={card.label}
            value={String(card.value)}
            sub=""
            tone={card.value > 0 ? (card.bad ? "bad" : "warn") : undefined}
            onClick={() => onNavigate(card.section)}
          />
        ))}
      </div>

      {money && !profit.isLoading ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="sb-dash-card">
            <h2 className="sb-dash-h">Revenue, carrier cost and profit - last 14 days</h2>
            <RevenueChart days={days} />
          </section>
          <section className="sb-dash-card">
            <h2 className="sb-dash-h">Where each customer's money goes - this month</h2>
            <CustomerChart orgs={orgs.data?.orgs ?? []} />
          </section>
        </div>
      ) : null}

      <section className="sb-dash-card">
        <h2 className="sb-dash-h">Needs action</h2>
        {waiting.length === 0 ? (
          <p className={MUTED}>Nothing is waiting on you.</p>
        ) : (
          <ul className="space-y-1.5">
            {waiting.map((item) => (
              <li key={item.section}>
                <button
                  type="button"
                  className="text-left text-[13px] hover:underline"
                  onClick={() => onNavigate(item.section)}
                >
                  {item.action(item.value)}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {problems.length > 0 ? (
        <div className="space-y-1">
          {problems.map((problem) => (
            <p key={problem} className="text-sm text-[hsl(var(--cx-muted))]">
              {problem}
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}
