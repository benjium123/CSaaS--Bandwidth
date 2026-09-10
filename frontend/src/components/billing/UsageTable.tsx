import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getErrorMessage } from "@/api/spend";
import {
  fetchUsageCall,
  formatCredits,
  formatQuantity,
  metricLabel,
  monthToDateRange,
  usageLines,
  usageTotalMicros,
  useUsageSummary,
  type UsageSummary,
} from "@/api/billing";
import { useAuth } from "@/auth/AuthContext";
import { Button, Drawer, EmptyState, Section, Spinner } from "@/components/ui/primitives";

function CallUsageDrawerBody({ callId }: { callId: string }) {
  const { api } = useAuth();
  const callQuery = useQuery({
    queryKey: ["billing", "usage", "call", callId],
    queryFn: () => fetchUsageCall(api, callId),
    enabled: Boolean(callId),
  });

  if (callQuery.isLoading) {
    return <Spinner label="Loading call usage" />;
  }

  if (callQuery.isError) {
    return (
      <div role="alert">
        <p className="text-sm text-destructive">{getErrorMessage(callQuery.error)}</p>
        <Button type="button" variant="outline" onClick={() => void callQuery.refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  const detail = callQuery.data;
  if (detail == null) {
    return <EmptyState title="Nothing was recorded for this call." />;
  }

  const events = detail.events ?? [];
  const callTotal =
    typeof detail.total_price_micros === "number"
      ? detail.total_price_micros
      : events.reduce((total, event) => total + event.price_micros, 0);

  return (
    <div className="space-y-3">
      {detail.seconds != null ? (
        <p className="text-sm text-muted-foreground">
          {(detail.seconds / 60).toFixed(1)} minutes
        </p>
      ) : null}

      {events.length === 0 ? (
        <EmptyState title="Nothing was recorded for this call." />
      ) : (
        <table className="w-full text-sm">
          <caption className="sr-only">Call usage</caption>
          <thead>
            <tr>
              <th className="text-left font-medium">What you used</th>
              <th className="text-left font-medium">Amount</th>
              <th className="text-right font-medium">Price</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id ?? `${event.metric}-${event.quantity}-${event.price_micros}`}>
                <td>{metricLabel(event.metric)}</td>
                <td>{formatQuantity(event.metric, event.quantity)}</td>
                <td className="text-right">{formatCredits(event.price_micros)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>Total for this call</td>
              <td />
              <td className="text-right">{formatCredits(callTotal)}</td>
            </tr>
          </tfoot>
        </table>
      )}
    </div>
  );
}

function UsageSummaryView({
  summary,
  onSelectCall,
}: {
  summary: UsageSummary;
  onSelectCall: (callId: string) => void;
}) {
  const lines = usageLines(summary);

  if (lines.length === 0) {
    return (
      <EmptyState
        title="Nothing used yet."
        description="Usage appears here as soon as your assistant takes a call."
      />
    );
  }

  return (
    <div className="space-y-4">
      <table className="w-full text-sm">
        <caption className="sr-only">Usage this month</caption>
        <thead>
          <tr>
            <th className="text-left font-medium">What you used</th>
            <th className="text-left font-medium">Amount</th>
            <th className="text-right font-medium">Price</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.metric}>
              <td>{metricLabel(line.metric)}</td>
              <td>{formatQuantity(line.metric, line.quantity)}</td>
              <td className="text-right">{formatCredits(line.price_micros)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>Total</td>
            <td />
            <td className="text-right">{formatCredits(usageTotalMicros(summary))}</td>
          </tr>
        </tfoot>
      </table>

      {summary.calls != null && summary.calls.length > 0 ? (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">Calls</h3>
          {summary.calls.map((call) => {
            const dateText = call.occurred_at
              ? new Date(call.occurred_at).toLocaleString()
              : "an unknown date";
            const priceText = formatCredits(call.price_micros);

            return (
              <Button
                key={call.call_id}
                type="button"
                variant="ghost"
                className="w-full justify-start"
                aria-label={`Call on ${dateText}, ${priceText}`}
                onClick={() => onSelectCall(call.call_id)}
              >
                <span className="flex-1 text-left">{dateText}</span>
                <span>{priceText}</span>
              </Button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

export function UsageTable() {
  const { api } = useAuth();
  const range = useMemo(() => monthToDateRange(), []);
  const usageQuery = useUsageSummary(api, range.from, range.to);
  const [openCallId, setOpenCallId] = useState<string | null>(null);

  return (
    <Section title="Usage this month">
      {usageQuery.isLoading ? <Spinner label="Loading usage" /> : null}

      {usageQuery.isError ? (
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(usageQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void usageQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : null}

      {usageQuery.data != null ? (
        <UsageSummaryView
          summary={usageQuery.data}
          onSelectCall={(callId) => setOpenCallId(callId)}
        />
      ) : null}

      <Drawer
        open={openCallId !== null}
        onClose={() => setOpenCallId(null)}
        title="What this call used"
      >
        {openCallId !== null ? <CallUsageDrawerBody callId={openCallId} /> : null}
      </Drawer>
    </Section>
  );
}
