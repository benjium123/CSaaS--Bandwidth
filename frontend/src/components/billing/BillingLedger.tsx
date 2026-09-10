import { useEffect, useState } from "react";
import { getErrorMessage } from "@/api/spend";
import {
  formatCredits,
  useLedger,
  type LedgerEntry,
} from "@/api/billing";
import { useAuth } from "@/auth/AuthContext";
import { Button, EmptyState, Section, Spinner } from "@/components/ui/primitives";

const ENTRY_TYPE_COPY: Record<string, string> = {
  topup: "Credits added",
  usage: "Used by your assistant",
  adjustment: "Adjustment",
  refund: "Refund",
  reserve: "Held for a call in progress",
  release: "Hold released",
};

function entryTypeLabel(type: string): string {
  return ENTRY_TYPE_COPY[type] ?? type.replace(/_/g, " ");
}

export function BillingLedger() {
  const { api } = useAuth();
  const [cursor, setCursor] = useState<string | null>(null);
  const [rows, setRows] = useState<LedgerEntry[]>([]);

  const ledgerQuery = useLedger(api, cursor);
  const hasRows = rows.length > 0;
  const nextCursor = ledgerQuery.data?.next_cursor ?? null;

  useEffect(() => {
    if (ledgerQuery.data == null) return;
    const items = ledgerQuery.data.items;
    setRows((current) => {
      if (cursor === null) return items;
      // Append by id rather than blindly concatenating: ANY refetch of the current page (a
      // window-focus refetch, the invalidation after a top-up) hands this effect the same
      // page again, and a plain concat would show every one of those entries twice.
      // Ledger ids are stable, so filtering on them is idempotent without a ref that has
      // to stay in step with React's lazily-run state updater.
      const seen = new Set(current.map((row) => row.id));
      return [...current, ...items.filter((row) => !seen.has(row.id))];
    });
  }, [ledgerQuery.data, cursor]);

  return (
    <Section title="Activity" description="Every credit added and used.">
      {ledgerQuery.isLoading && !hasRows ? <Spinner label="Loading activity" /> : null}

      {ledgerQuery.isError ? (
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(ledgerQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void ledgerQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : null}

      {!ledgerQuery.isLoading &&
      !ledgerQuery.isError &&
      ledgerQuery.data != null &&
      ledgerQuery.data.items.length === 0 &&
      !hasRows ? (
        <EmptyState
          title="Nothing here yet."
          description="Credits you add and use will show up here."
        />
      ) : null}

      {hasRows ? (
        <>
          <table className="w-full text-sm">
            <caption className="sr-only">Activity</caption>
            <thead>
              <tr>
                <th className="text-left font-medium">When</th>
                <th className="text-left font-medium">What happened</th>
                <th className="text-left font-medium">Note</th>
                <th className="text-left font-medium">Amount</th>
                <th className="text-left font-medium">Balance</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.created_at).toLocaleString()}</td>
                  <td>{entryTypeLabel(entry.entry_type)}</td>
                  <td>{entry.note ?? "—"}</td>
                  <td
                    className={entry.amount_micros < 0 ? "text-destructive" : undefined}
                  >
                    {formatCredits(entry.amount_micros)}
                  </td>
                  <td>{formatCredits(entry.balance_after_micros)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {nextCursor != null ? (
            <Button
              type="button"
              variant="outline"
              disabled={ledgerQuery.isFetching}
              onClick={() => setCursor(nextCursor)}
            >
              Load more
            </Button>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}
