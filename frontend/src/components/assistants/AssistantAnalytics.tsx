import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  ANALYTICS_RANGE_DAYS,
  ASSISTANT_ANALYTICS_KEY,
  analyticsRange,
  fetchAssistantAnalytics,
  formatCostMicros,
  formatMinutes,
  formatRate,
  formatSeconds,
  type AnalyticsRangeDays,
} from "@/api/assistantOps";
import {
  EmptyState,
  Section,
  Spinner,
} from "@/components/ui/primitives";
import { ConsoleCard, FilterPill } from "@/components/ui/consoleChrome";

export function AssistantAnalyticsStrip({
  days,
  className,
}: {
  days: number;
  className?: string;
  // F2: the `!data` guard below cannot be narrowed away by TypeScript, so the signature has
  // to admit null. It is unreachable in practice - isPending covers "no data yet".
}): React.JSX.Element | null {
  const { api } = useAuth();
  const range = React.useMemo(() => analyticsRange(days), [days]);
  const query = useQuery({
    queryKey: [...ASSISTANT_ANALYTICS_KEY, range.from, range.to],
    queryFn: () => fetchAssistantAnalytics(api, range),
  });

  if (query.isPending) {
    return <Spinner label="Loading assistant activity" />;
  }

  if (query.isError) {
    return (
      <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
        {(query.error as Error).message}
      </p>
    );
  }

  const data = query.data;
  if (!data) return null;

  if (data.calls === 0) {
    return (
      <EmptyState
        title="No assistant calls yet"
        description="Once your assistant takes a call, its activity shows up here."
      />
    );
  }

  const cost = formatCostMicros(data.cost_micros);
  const tiles: { label: string; value: string | number }[] = [
    { label: "Calls", value: data.calls },
    { label: "Minutes", value: formatMinutes(data.minutes) },
    { label: "Answered", value: formatRate(data.answer_rate) },
    { label: "Passed to a person", value: formatRate(data.handoff_rate) },
    { label: "Booked", value: data.booked },
    {
      label: "Average call",
      value: formatSeconds(data.avg_duration_seconds),
    },
  ];

  if (cost) {
    tiles.push({ label: "Cost", value: cost });
  }

  return (
    <div
      role="list"
      className={`grid grid-cols-2 gap-[11px] sm:grid-cols-3 lg:grid-cols-6 ${
        className ?? ""
      }`}
    >
      {tiles.map((tile) => (
        <div role="listitem" key={tile.label}>
          <ConsoleCard className="space-y-[3px]">
            <p className="text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
              {tile.label}
            </p>
            <p className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
              {tile.value}
            </p>
          </ConsoleCard>
        </div>
      ))}
    </div>
  );
}

export function AssistantAnalyticsPanel(): React.JSX.Element {
  const [days, setDays] = React.useState<AnalyticsRangeDays>(30);

  return (
    <Section
      title="Activity"
      description="Across all of your assistants."
      actions={
        <div role="group" aria-label="Range" className="flex gap-[7px]">
          {ANALYTICS_RANGE_DAYS.map((opt) => (
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
      }
    >
      <AssistantAnalyticsStrip days={days} />
    </Section>
  );
}
