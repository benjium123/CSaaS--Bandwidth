import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  daysInRange,
  formatDateShort,
  formatMicros,
  lastNDaysRange,
  monthToDateRange,
  useSpendDaily,
  useSpendSummary,
  type SpendMetric,
  type SpendMetricLine,
} from "@/api/spend";

export const SPEND_METRIC_LABELS: Record<SpendMetric, string> = {
  sms_out: "SMS out",
  sms_in: "SMS in",
  mms_out: "MMS out",
  mms_in: "MMS in",
  voice_min_out: "Voice min out",
  voice_min_in: "Voice min in",
  number_mrc: "Number MRC",
  number_setup: "Number setup",
};

export function SpendCard({
  provider,
  onOpenRates,
}: {
  provider: string;
  // When provided, the "no rate card" note's "(set rates)" text becomes a real button
  // that opens the Rates drawer (owned by the caller - ProvidersPage). Omit it and the
  // note still renders, just as plain text, for a caller with no drawer to open.
  onOpenRates?: () => void;
}) {
  const { api } = useAuth();
  const range = React.useMemo(() => monthToDateRange(), []);
  const summaryQuery = useSpendSummary(api, range.from, range.to);
  const providerSpend = summaryQuery.data?.by_provider[provider];
  const isUnrated = (summaryQuery.data?.unrated_providers ?? []).includes(provider);
  const [expanded, setExpanded] = React.useState(false);

  const metricEntries = React.useMemo(() => {
    if (!providerSpend) return [];
    return (Object.entries(providerSpend.by_metric) as [SpendMetric, SpendMetricLine][]).sort(
      ([a], [b]) => a.localeCompare(b),
    );
  }, [providerSpend]);

  return (
    <div className="border-t border-neutral-800 pt-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs font-medium text-neutral-400">
          Spend this month <span className="text-neutral-600">(UTC days)</span>
        </p>
        {providerSpend && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={`${provider}-spend-breakdown`}
            onClick={() => setExpanded((prev) => !prev)}
            className="text-xs text-neutral-500 underline hover:text-neutral-300"
          >
            {expanded ? "Hide breakdown" : "Show breakdown"}
          </button>
        )}
      </div>

      {summaryQuery.isLoading ? (
        <p className="text-xs text-neutral-500">Loading spend…</p>
      ) : summaryQuery.isError ? (
        <p className="text-xs text-red-400">Spend unavailable</p>
      ) : !providerSpend ? (
        <p className="text-xs text-neutral-500">No spend yet.</p>
      ) : (
        <>
          <p className="text-sm font-semibold text-neutral-100">
            {formatMicros(providerSpend.cost_micros)}
          </p>

          {isUnrated && (
            <p className="mt-1 text-[10px] text-amber-400">
              No rate card — costs shown as $0.00{" "}
              {onOpenRates ? (
                <button
                  type="button"
                  onClick={onOpenRates}
                  className="underline hover:text-amber-300"
                >
                  (set rates)
                </button>
              ) : (
                "(set rates)"
              )}
            </p>
          )}

          {expanded && (
            <div id={`${provider}-spend-breakdown`} className="mt-3 space-y-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-neutral-500">
                    <th className="py-1 font-medium">Metric</th>
                    <th className="py-1 text-right font-medium">Qty</th>
                    <th className="py-1 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {metricEntries.map(([metric, line]) => (
                    <tr key={metric} className="border-t border-neutral-800">
                      <td className="py-1 text-neutral-300">{SPEND_METRIC_LABELS[metric]}</td>
                      <td className="py-1 text-right text-neutral-400">{line.quantity}</td>
                      <td className="py-1 text-right text-neutral-200">
                        {formatMicros(line.cost_micros)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {providerSpend.numbers.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-neutral-400">Numbers</p>
                  <ul className="mt-1 space-y-1">
                    {providerSpend.numbers.map((number) => (
                      <li
                        key={number.number_id}
                        className="flex justify-between text-xs text-neutral-300"
                      >
                        <span>{number.e164}</span>
                        <span>{formatMicros(number.cost_micros)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function SpendTile() {
  const { api } = useAuth();
  const mtdRange = React.useMemo(() => monthToDateRange(), []);
  const dailyRange = React.useMemo(() => lastNDaysRange(30), []);
  const summaryQuery = useSpendSummary(api, mtdRange.from, mtdRange.to);
  const dailyQuery = useSpendDaily(api, dailyRange.from, dailyRange.to);

  const providerEntries = React.useMemo(() => {
    return Object.entries(summaryQuery.data?.by_provider ?? {}).sort(
      ([, a], [, b]) => b.cost_micros - a.cost_micros,
    );
  }, [summaryQuery.data]);

  // Zero-fill every UTC day in the requested range BEFORE folding in the returned rows, so
  // a day with no spend still gets its own (zero-height) bar - the chart always shows
  // exactly 30 bars instead of only the days the backend happened to return rows for. The
  // `map.has` guard means a row whose period_date falls outside the requested range (a
  // backend bug, clock skew, etc.) is dropped instead of appending a 31st bar.
  const dailyByDate = React.useMemo(() => {
    const map = new Map<string, number>();
    for (const day of daysInRange(dailyRange.from, dailyRange.to)) {
      map.set(day, 0);
    }
    (dailyQuery.data ?? []).forEach((row) => {
      if (!map.has(row.period_date)) return;
      map.set(row.period_date, (map.get(row.period_date) ?? 0) + row.cost_micros);
    });
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [dailyQuery.data, dailyRange.from, dailyRange.to]);

  const maxDailyMicros = React.useMemo(() => {
    return Math.max(1, ...dailyByDate.map(([, micros]) => micros));
  }, [dailyByDate]);

  const allDailyZero = dailyByDate.every(([, micros]) => micros === 0);

  // Matches ChartCard's token-based shell in DashboardPage.tsx (border-border,
  // text-muted-foreground/text-destructive) rather than a hardcoded dark palette - this
  // page doesn't force a "dark" class the way ProvidersPage/NumbersPage do, it follows
  // the viewer's light/dark theme via CSS variables.
  if (summaryQuery.isLoading || dailyQuery.isLoading) {
    return (
      <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
        Loading spend…
      </div>
    );
  }

  if (summaryQuery.isError) {
    return (
      <div role="alert" className="rounded-md border border-border p-4 text-sm text-destructive">
        Spend data is unavailable.
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border p-4">
      <h2 className="mb-1 text-sm font-medium">Spend</h2>
      <p className="mb-2 text-xs text-muted-foreground">Month-to-date, UTC days</p>
      <p className="text-2xl font-semibold">
        {formatMicros(summaryQuery.data?.total_micros ?? 0)}
      </p>

      <ul className="mt-3 space-y-1">
        {providerEntries.map(([provider, spend]) => (
          <li key={provider} className="flex justify-between text-xs text-muted-foreground">
            <span>{provider}</span>
            <span className="text-foreground">{formatMicros(spend.cost_micros)}</span>
          </li>
        ))}
      </ul>

      {(summaryQuery.data?.unrated_providers ?? []).length > 0 && (
        <p className="mt-2 text-xs text-amber-500">
          No rate card: {(summaryQuery.data?.unrated_providers ?? []).join(", ")}
        </p>
      )}

      <div className="mt-5">
        <h3 className="mb-2 text-xs font-medium text-muted-foreground">Last 30 days</h3>
        {dailyQuery.isError ? (
          <p role="alert" className="text-sm text-destructive">
            Daily spend is unavailable.
          </p>
        ) : allDailyZero ? (
          <p className="text-sm text-muted-foreground">No spend data for this range.</p>
        ) : (
          <div className="flex h-24 items-end gap-px">
            {dailyByDate.map(([date, micros]) => {
              const heightPct = Math.round((micros / maxDailyMicros) * 100);
              return (
                <div key={date} className="flex h-full flex-1 items-end">
                  <div
                    role="img"
                    aria-label={`Spend ${date}: ${formatMicros(micros)}`}
                    title={`${formatDateShort(date)}: ${formatMicros(micros)}`}
                    className="w-full rounded-sm bg-primary"
                    style={{ height: `${heightPct}%` }}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
