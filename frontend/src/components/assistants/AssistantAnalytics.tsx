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
  Button,
  Card,
  EmptyState,
  Section,
  Spinner,
} from "@/components/ui/primitives";

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
      <p role="alert" className="text-sm text-destructive">
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
      className={`grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6 ${
        className ?? ""
      }`}
    >
      {tiles.map((tile) => (
        <div role="listitem" key={tile.label}>
          <Card className="space-y-1">
            <p className="text-xs text-muted-foreground">{tile.label}</p>
            <p className="text-lg font-semibold text-foreground">{tile.value}</p>
          </Card>
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
        <div role="group" aria-label="Range" className="flex gap-2">
          {ANALYTICS_RANGE_DAYS.map((opt) => (
            <Button
              key={opt}
              type="button"
              variant={days === opt ? "default" : "outline"}
              size="sm"
              onClick={() => setDays(opt)}
            >
              {opt}d
            </Button>
          ))}
        </div>
      }
    >
      <AssistantAnalyticsStrip days={days} />
    </Section>
  );
}
