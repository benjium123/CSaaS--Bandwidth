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
import {
  useAnalyticsOverview,
  useTranscriptSearch,
  type AnalyticsOverviewOut,
} from "@/api/hooks";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const RANGE_OPTIONS = [7, 30, 90] as const;

// A brand-neutral placeholder palette - swap for the console's own chart tokens later.
const COLORS = {
  inbound: "#2563eb",
  outbound: "#16a34a",
  deliveryRate: "#f59e0b",
  calls: "#2563eb",
  avgDuration: "#f97316",
  turns: "#2563eb",
  handoffs: "#dc2626",
};

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
    <div className="rounded-md border border-border p-4">
      <h2 className="mb-3 text-sm font-medium">{title}</h2>
      {empty ? (
        <p className="text-sm text-muted-foreground">No data for this range.</p>
      ) : (
        <div style={{ width: "100%", height: 220 }}>{children}</div>
      )}
    </div>
  );
}

function MessagesChart({ data }: { data: AnalyticsOverviewOut["messages"] }) {
  const chartData = data.map((d) => ({ ...d, label: formatDateShort(d.date) }));
  return (
    <ChartCard title="Messages in / out" empty={data.length === 0}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip />
          <Legend />
          <Line
            type="monotone"
            dataKey="inbound"
            name="Inbound"
            stroke={COLORS.inbound}
            strokeWidth={2}
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="outbound"
            name="Outbound"
            stroke={COLORS.outbound}
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function DeliveryRateChart({ data }: { data: AnalyticsOverviewOut["messages"] }) {
  const chartData = data.map((d) => ({
    label: formatDateShort(d.date),
    // Terminal-status ratio only (P13 DR-10) - a day with no terminal outbound sends yet
    // has no rate to plot, so it stays a gap rather than a misleading zero.
    delivery_rate: d.delivery_rate == null ? null : Math.round(d.delivery_rate * 1000) / 10,
  }));
  const hasAny = chartData.some((d) => d.delivery_rate != null);
  return (
    <ChartCard title="Delivery rate" empty={!hasAny}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} domain={[0, 100]} unit="%" />
          <Tooltip formatter={(v: number) => `${v}%`} />
          <Line
            type="monotone"
            dataKey="delivery_rate"
            name="Delivery rate"
            stroke={COLORS.deliveryRate}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function CallsChart({ data }: { data: AnalyticsOverviewOut["calls"] }) {
  const chartData = data.map((d) => ({
    label: formatDateShort(d.date),
    calls: d.calls,
    avg_duration_seconds: d.avg_duration_seconds == null ? null : Math.round(d.avg_duration_seconds),
  }));
  return (
    <ChartCard title="Calls + avg duration" empty={data.length === 0}>
      <ResponsiveContainer>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis yAxisId="left" fontSize={11} allowDecimals={false} />
          <YAxis yAxisId="right" orientation="right" fontSize={11} unit="s" />
          <Tooltip />
          <Legend />
          <Bar yAxisId="left" dataKey="calls" name="Calls" fill={COLORS.calls} />
          <Line
            yAxisId="right"
            type="monotone"
            dataKey="avg_duration_seconds"
            name="Avg duration (s)"
            stroke={COLORS.avgDuration}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function AiChart({ data }: { data: AnalyticsOverviewOut["ai"] }) {
  const chartData = data.map((d) => ({ ...d, label: formatDateShort(d.date) }));
  return (
    <ChartCard title="AI turns + handoffs" empty={data.length === 0}>
      <ResponsiveContainer>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip />
          <Legend />
          <Line
            type="monotone"
            dataKey="turns"
            name="Turns"
            stroke={COLORS.turns}
            strokeWidth={2}
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="handoffs"
            name="Handoffs"
            stroke={COLORS.handoffs}
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function CampaignsChart({ data }: { data: AnalyticsOverviewOut["campaigns"] }) {
  return (
    <ChartCard title="Campaign progress (current)" empty={data.length === 0}>
      <ResponsiveContainer>
        <ComposedChart data={data} layout="vertical">
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis type="number" fontSize={11} allowDecimals={false} />
          <YAxis type="category" dataKey="status" fontSize={11} width={90} />
          <Tooltip />
          <Bar dataKey="count" name="Campaigns" fill={COLORS.calls} />
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
    <div className="rounded-md border border-border p-4">
      <h2 className="mb-3 text-sm font-medium">Transcript search</h2>
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
            <p className="text-sm text-muted-foreground">No matching transcripts.</p>
          ) : (
            <ul className="space-y-3">
              {(results ?? []).map((r) => (
                <li key={r.call_id} className="rounded-md border border-border p-3 text-sm">
                  <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
                    <span>{formatPhone(r.contact_e164)}</span>
                    <span>{new Date(r.started_at).toLocaleString()}</span>
                  </div>
                  <ul className="space-y-1">
                    {r.segments.map((seg, i) => (
                      <li
                        key={i}
                        className={cn(
                          "text-xs",
                          seg.matched ? "font-medium text-foreground" : "text-muted-foreground",
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
    </div>
  );
}

export function DashboardPage() {
  const { api } = useAuth();
  const [days, setDays] = React.useState<(typeof RANGE_OPTIONS)[number]>(30);
  const { data, isLoading, error } = useAnalyticsOverview(api, days);

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Dashboard</h1>
        <div className="flex gap-1" role="group" aria-label="Range">
          {RANGE_OPTIONS.map((opt) => (
            <Button
              key={opt}
              type="button"
              size="sm"
              variant={days === opt ? "default" : "outline"}
              onClick={() => setDays(opt)}
            >
              {opt}d
            </Button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <Spinner label="Loading analytics" />
      ) : error ? (
        <p role="alert" className="text-sm text-destructive">
          {(error as Error).message}
        </p>
      ) : !data ? null : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <MessagesChart data={data.messages} />
          <DeliveryRateChart data={data.messages} />
          <CallsChart data={data.calls} />
          <AiChart data={data.ai} />
          <div className="md:col-span-2">
            <CampaignsChart data={data.campaigns} />
          </div>
        </div>
      )}

      <TranscriptSearch api={api} />
    </div>
  );
}
