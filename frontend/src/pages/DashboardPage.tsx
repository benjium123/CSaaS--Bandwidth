import * as React from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useAuth } from "@/auth/AuthContext";
import { useSurfaceTheme } from "@/auth/useSurfaceTheme";
import {
  useAnalyticsOverview,
  useTranscriptSearch,
  type AnalyticsOverviewOut,
} from "@/api/hooks";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { FilterPill, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { SpendTile } from "@/components/spend/SpendCard";
import { AssistantAnalyticsStrip } from "@/components/assistants/AssistantAnalytics";
import { MessagingHealthCard } from "@/components/messaging/MessagingHealthCard";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const RANGE_OPTIONS = [7, 30, 90] as const;

/**
 * THE CHARTS USED TO BE THEME-BLIND IN BOTH THEMES.
 *
 * Two separate bugs. The series were seven hardcoded hex values - a placeholder palette
 * that included TWO oranges (#f59e0b, #f97316), which docs/design/console-reference.html
 * rules out in as many words: "No orange anywhere". And the grid, the axes, the tick
 * labels, the legend and the tooltip carried no colour at all, so Recharts fell back to its
 * built-in LIGHT defaults (#ccc grid, #666 ticks, a white tooltip card) - unreadable on the
 * dark console and not matching the light one either, because they are Recharts' greys and
 * not ours.
 *
 * Recharts takes colours as SVG paint strings, not as classes, so the palette has to be
 * read rather than applied. The `--cx-*` custom properties are scoped to `.console-surface`
 * (see consoleTheme.css), NOT to :root - so this reads them off an element inside the page,
 * via a ref, and re-reads whenever the stored theme flips. The values are HSL triplets
 * ("219.7 82.2% 64.7%"), so each one is wrapped back into `hsl(...)`.
 *
 * The fallbacks below are the DARK reference values, used when the properties cannot be
 * resolved - which is exactly what happens under vitest, where `css: false` means no
 * stylesheet is ever loaded. No test can prove a colour here; the fallback only keeps the
 * charts from rendering with `undefined` paint in that environment.
 */
const CHART_TOKENS = {
  accent: "--cx-accent",
  live: "--cx-live",
  flag: "--cx-flag",
  danger: "--cx-danger",
  line: "--cx-line",
  muted: "--cx-muted",
  surface: "--cx-surface",
  text: "--cx-text",
} as const;

type ChartPalette = Record<keyof typeof CHART_TOKENS, string>;

/** The reference's dark set, as literals, for the no-stylesheet case only. */
const FALLBACK_PALETTE: ChartPalette = {
  accent: "hsl(219.7 82.2% 64.7%)",
  live: "hsl(153.1 60.2% 52.7%)",
  flag: "hsl(43.1 73.6% 65.9%)",
  danger: "hsl(6 74% 62%)",
  line: "hsl(220 13.6% 17.3%)",
  muted: "hsl(214.7 7.9% 46.9%)",
  surface: "hsl(222 16.1% 12.2%)",
  text: "hsl(216 12.2% 92%)",
};

function useChartPalette(ref: React.RefObject<HTMLElement | null>): ChartPalette {
  // Not for its value but for its CHANGES: flipping the console theme re-points every
  // --cx-* on the surface, and the charts have to re-read to follow.
  const { theme } = useSurfaceTheme();
  const [palette, setPalette] = React.useState<ChartPalette>(FALLBACK_PALETTE);

  React.useEffect(() => {
    const el = ref.current;
    if (!el || typeof window === "undefined" || typeof window.getComputedStyle !== "function") {
      return;
    }
    const computed = window.getComputedStyle(el);
    const next = { ...FALLBACK_PALETTE };
    for (const [key, prop] of Object.entries(CHART_TOKENS) as Array<
      [keyof typeof CHART_TOKENS, string]
    >) {
      const raw = computed.getPropertyValue(prop).trim();
      if (raw) next[key] = `hsl(${raw})`;
    }
    // Compared before setting: this effect runs on every theme change and an unconditional
    // setState of a fresh object would re-render for no reason.
    setPalette((prev) =>
      (Object.keys(next) as Array<keyof ChartPalette>).every((k) => prev[k] === next[k])
        ? prev
        : next,
    );
  }, [ref, theme]);

  return palette;
}

/** The series hues. Three of the reference's rationed colours plus its red; no orange. */
function seriesColors(p: ChartPalette) {
  return {
    inbound: p.accent,
    outbound: p.live,
    deliveryRate: p.flag,
    calls: p.accent,
    avgDuration: p.flag,
    turns: p.accent,
    handoffs: p.danger,
  };
}

/** The chrome every chart shares: grid, both axes, the tooltip card and the legend. */
function axisProps(p: ChartPalette) {
  return { stroke: p.muted, tick: { fill: p.muted } } as const;
}

function tooltipProps(p: ChartPalette) {
  return {
    contentStyle: {
      background: p.surface,
      border: `1px solid ${p.line}`,
      borderRadius: 14,
      color: p.text,
    },
    labelStyle: { color: p.text },
    itemStyle: { color: p.text },
    cursor: { stroke: p.muted },
  } as const;
}

function legendProps(p: ChartPalette) {
  return { wrapperStyle: { color: p.muted, fontSize: 11 } } as const;
}

function formatDateShort(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function ChartCard({
  title,
  empty,
  children,
}: {
  title: string;
  empty: boolean;
  children: React.ReactNode;
}) {
  return (
    <SurfaceCard className="p-5">
      <h2 className="mb-4 text-[13.5px] font-semibold tracking-[-0.01em] text-[hsl(var(--cx-text))]">
        {title}
      </h2>
      {empty ? (
        <p className="text-[13px] text-[hsl(var(--cx-muted))]">No data for this range.</p>
      ) : (
        <div style={{ width: "100%", height: 220 }}>{children}</div>
      )}
    </SurfaceCard>
  );
}

function MessagesChart({ data, p }: { data: AnalyticsOverviewOut["messages"]; p: ChartPalette }) {
  const chartData = data.map((d) => ({ ...d, label: formatDateShort(d.date) }));
  const series = seriesColors(p);
  return (
    <ChartCard title="Messages in / out" empty={data.length === 0}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke={p.line} />
          <XAxis dataKey="label" fontSize={11} {...axisProps(p)} />
          <YAxis fontSize={11} allowDecimals={false} {...axisProps(p)} />
          <Tooltip {...tooltipProps(p)} />
          <Legend {...legendProps(p)} />
          <Line
            type="monotone"
            dataKey="inbound"
            name="Inbound"
            stroke={series.inbound}
            strokeWidth={2}
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="outbound"
            name="Outbound"
            stroke={series.outbound}
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function DeliveryRateChart({
  data,
  p,
}: {
  data: AnalyticsOverviewOut["messages"];
  p: ChartPalette;
}) {
  const chartData = data.map((d) => ({
    label: formatDateShort(d.date),
    // Terminal-status ratio only (P13 DR-10) - a day with no terminal outbound sends yet
    // has no rate to plot, so it stays a gap rather than a misleading zero.
    delivery_rate: d.delivery_rate == null ? null : Math.round(d.delivery_rate * 1000) / 10,
  }));
  const hasAny = chartData.some((d) => d.delivery_rate != null);
  const series = seriesColors(p);
  return (
    <ChartCard title="Delivery rate" empty={!hasAny}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke={p.line} />
          <XAxis dataKey="label" fontSize={11} {...axisProps(p)} />
          <YAxis fontSize={11} domain={[0, 100]} unit="%" {...axisProps(p)} />
          <Tooltip {...tooltipProps(p)} formatter={(v: number) => `${v}%`} />
          <Line
            type="monotone"
            dataKey="delivery_rate"
            name="Delivery rate"
            stroke={series.deliveryRate}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function CallsChart({ data, p }: { data: AnalyticsOverviewOut["calls"]; p: ChartPalette }) {
  const chartData = data.map((d) => ({
    label: formatDateShort(d.date),
    calls: d.calls,
    avg_duration_seconds: d.avg_duration_seconds == null ? null : Math.round(d.avg_duration_seconds),
  }));
  const series = seriesColors(p);
  return (
    <ChartCard title="Calls + avg duration" empty={data.length === 0}>
      <ResponsiveContainer>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke={p.line} />
          <XAxis dataKey="label" fontSize={11} {...axisProps(p)} />
          <YAxis yAxisId="left" fontSize={11} allowDecimals={false} {...axisProps(p)} />
          <YAxis yAxisId="right" orientation="right" fontSize={11} unit="s" {...axisProps(p)} />
          <Tooltip {...tooltipProps(p)} />
          <Legend {...legendProps(p)} />
          <Bar yAxisId="left" dataKey="calls" name="Calls" fill={series.calls} />
          <Line
            yAxisId="right"
            type="monotone"
            dataKey="avg_duration_seconds"
            name="Avg duration (s)"
            stroke={series.avgDuration}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function AiChart({ data, p }: { data: AnalyticsOverviewOut["ai"]; p: ChartPalette }) {
  const chartData = data.map((d) => ({ ...d, label: formatDateShort(d.date) }));
  const series = seriesColors(p);
  return (
    <ChartCard title="AI turns + handoffs" empty={data.length === 0}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke={p.line} />
          <XAxis dataKey="label" fontSize={11} {...axisProps(p)} />
          <YAxis fontSize={11} allowDecimals={false} {...axisProps(p)} />
          <Tooltip {...tooltipProps(p)} />
          <Legend {...legendProps(p)} />
          <Line
            type="monotone"
            dataKey="turns"
            name="Turns"
            stroke={series.turns}
            strokeWidth={2}
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="handoffs"
            name="Handoffs"
            stroke={series.handoffs}
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function CampaignsChart({
  data,
  p,
}: {
  data: AnalyticsOverviewOut["campaigns"];
  p: ChartPalette;
}) {
  const series = seriesColors(p);
  return (
    <ChartCard title="Campaign progress (current)" empty={data.length === 0}>
      <ResponsiveContainer>
        <ComposedChart data={data} layout="vertical">
          <CartesianGrid strokeDasharray="3 3" stroke={p.line} />
          <XAxis type="number" fontSize={11} allowDecimals={false} {...axisProps(p)} />
          <YAxis type="category" dataKey="status" fontSize={11} width={90} {...axisProps(p)} />
          <Tooltip {...tooltipProps(p)} />
          <Bar dataKey="count" name="Campaigns" fill={series.calls} />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function TranscriptSearch({ api }: { api: ReturnType<typeof useAuth>["api"] }) {
  const [q, setQ] = React.useState("");
  const [submitted, setSubmitted] = React.useState("");
  const { data: results, isLoading, isFetching, error } = useTranscriptSearch(
    api,
    submitted,
    Boolean(submitted),
  );

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitted(q.trim());
  }

  return (
    <SurfaceCard className="p-5">
      <h2 className="mb-4 text-[13.5px] font-semibold tracking-[-0.01em] text-[hsl(var(--cx-text))]">
        Transcript search
      </h2>
      <form className="flex gap-2" onSubmit={onSubmit}>
        <Input
          aria-label="Search transcripts"
          placeholder="Search call transcripts…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Button type="submit" disabled={!q.trim()}>
          Search
        </Button>
      </form>

      {submitted && (
        <div className="mt-4">
          {isLoading || isFetching ? (
            <Spinner label="Searching" />
          ) : error ? (
            <p role="alert" className="text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (results ?? []).length === 0 ? (
            <p className="text-[13px] text-[hsl(var(--cx-muted))]">No matching transcripts.</p>
          ) : (
            <ul className="space-y-3">
              {(results ?? []).map((r) => (
                <li
                  key={r.call_id}
                  className="rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-4 text-sm"
                >
                  <div className="mb-2 flex items-center justify-between text-[11.5px] text-[hsl(var(--cx-muted))]">
                    <span>{formatPhone(r.contact_e164)}</span>
                    <span>{new Date(r.started_at).toLocaleString()}</span>
                  </div>
                  <ul className="space-y-1">
                    {r.segments.map((seg, i) => (
                      <li
                        key={i}
                        className={cn(
                          "text-[12px] leading-[1.6]",
                          seg.matched
                            ? "font-semibold text-[hsl(var(--cx-text))]"
                            : "text-[hsl(var(--cx-subtle))]",
                        )}
                      >
                        <span className="uppercase">{seg.role}:</span> {seg.text}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </SurfaceCard>
  );
}

export function DashboardPage() {
  const { api } = useAuth();
  const [days, setDays] = React.useState<(typeof RANGE_OPTIONS)[number]>(30);
  const { data, isLoading, error } = useAnalyticsOverview(api, days);
  // The ref is what the palette is read off: --cx-* lives on the `.console-surface`
  // ancestor, so a computed style is only meaningful from an element inside the page.
  const rootRef = React.useRef<HTMLDivElement>(null);
  const p = useChartPalette(rootRef);

  return (
    <div ref={rootRef} className="mx-auto max-w-5xl space-y-5 p-6 sm:p-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <SectionLabel>Overview</SectionLabel>
          <h1 className="mt-1 text-[24px] font-semibold tracking-[-0.02em] text-[hsl(var(--cx-text))]">
            Dashboard
          </h1>
        </div>
        {/* The reference's `.pills`: 999px chips, the selected one on a solid accent
            fill. `aria-pressed` is what makes the selected range audible; the buttons
            keep the names ("7d", "30d", "90d") the suites find them by. */}
        <div
          className="flex gap-[7px] rounded-full bg-[hsl(var(--cx-surface))] p-1"
          role="group"
          aria-label="Range"
        >
          {RANGE_OPTIONS.map((opt) => (
            <FilterPill
              key={opt}
              active={days === opt}
              aria-pressed={days === opt}
              onClick={() => setDays(opt)}
            >
              {opt}d
            </FilterPill>
          ))}
        </div>
      </div>

      {isLoading ? (
        <SurfaceCard className="flex items-center justify-center py-10">
          <Spinner label="Loading analytics" />
        </SurfaceCard>
      ) : error ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[hsl(var(--cx-danger)/0.4)] bg-[hsl(var(--cx-danger)/0.09)] px-4 py-3 text-[13px] text-[hsl(var(--cx-danger))]"
        >
          {(error as Error).message}
        </p>
      ) : !data ? null : (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <MessagesChart data={data.messages} p={p} />
          <DeliveryRateChart data={data.messages} p={p} />
          <CallsChart data={data.calls} p={p} />
          <AiChart data={data.ai} p={p} />
          <div className="md:col-span-2">
            {/* Shares the page's own range buttons rather than carrying a second set - two
                range controls on one screen is how a dashboard starts lying to you. */}
            <SurfaceCard className="p-5">
              <h2 className="mb-4 text-[13.5px] font-semibold tracking-[-0.01em] text-[hsl(var(--cx-text))]">
                Assistant activity
              </h2>
              <AssistantAnalyticsStrip days={days} />
            </SurfaceCard>
          </div>
          <div className="md:col-span-2">
            <SpendTile />
          </div>
          <div className="md:col-span-2">
            <MessagingHealthCard days={days} />
          </div>
          <div className="md:col-span-2">
            {/* Both sides kept: main's messaging-health card above, and the campaigns
                chart which now takes the console palette (`p`) like every other chart
                on this page. */}
            <CampaignsChart data={data.campaigns} p={p} />
          </div>
        </div>
      )}

      <TranscriptSearch api={api} />
    </div>
  );
}
