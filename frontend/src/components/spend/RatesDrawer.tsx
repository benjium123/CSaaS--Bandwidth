import * as React from "react";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import {
  dollarsToMicros,
  getErrorMessage,
  microsToDollars,
  todayUTC,
  useProviderRates,
  useRollupDay,
  useUpdateRates,
  type ProviderRate,
  type SpendMetric,
} from "@/api/spend";

function rateKey(provider: string, metric: SpendMetric): string {
  return `${provider}:${metric}`;
}

function isDraftInvalid(value: string): boolean {
  const v = Number(value);
  return value.trim() === "" || !Number.isFinite(v) || v < 0;
}

function DrawerMutationStatus({
  mutation,
  pendingLabel = "Saving…",
  successLabel = "Saved",
}: {
  mutation: { isPending: boolean; isError: boolean; isSuccess: boolean; error: unknown };
  pendingLabel?: string;
  successLabel?: string;
}) {
  if (mutation.isPending) {
    return (
      <span className="flex items-center gap-1 text-[10px] text-muted-foreground/75">
        <Loader2 className="h-3 w-3 animate-spin" />
        {pendingLabel}
      </span>
    );
  }
  if (mutation.isError) {
    return (
      <span role="alert" className="text-[10px] text-destructive">
        {getErrorMessage(mutation.error)}
      </span>
    );
  }
  if (mutation.isSuccess) {
    return <span className="text-[10px] text-[hsl(var(--cx-live))]">{successLabel}</span>;
  }
  return null;
}

export function RatesDrawer({
  readOnly,
  onClose,
}: {
  readOnly: boolean;
  onClose: () => void;
}) {
  const { api } = useAuth();
  const ratesQuery = useProviderRates(api);
  const updateRates = useUpdateRates(api);
  const rollupDay = useRollupDay(api);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});

  const closeButtonRef = React.useRef<HTMLButtonElement>(null);

  const rows = ratesQuery.data ?? [];

  const changedRows = React.useMemo(() => {
    return rows.flatMap((row) => {
      const draft = drafts[rateKey(row.provider, row.metric)];
      if (draft == null) return [];
      const value = Number(draft);
      if (!Number.isFinite(value)) return [];
      const micros = dollarsToMicros(value);
      if (micros === row.unit_cost_micros) return [];
      return [{ provider: row.provider, metric: row.metric, unit_cost_micros: micros }];
    });
  }, [drafts, rows]);

  const isDirty = changedRows.length > 0;

  // Seed `drafts` from the fetched rows on load and after any background refetch that
  // lands with no unsaved edits in flight - but never clobber a row the operator is
  // mid-edit on just because react-query refetched in the background (e.g. window focus,
  // or the periodic refetch after a Recalculate/Save on ANOTHER row's mutation settles).
  React.useEffect(() => {
    if (isDirty) return;
    const next: Record<string, string> = {};
    (ratesQuery.data ?? []).forEach((row) => {
      next[rateKey(row.provider, row.metric)] = microsToDollars(row.unit_cost_micros).toFixed(4);
    });
    setDrafts(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ratesQuery.data]);

  const hasInvalidDraft = rows.some((row) =>
    isDraftInvalid(drafts[rateKey(row.provider, row.metric)] ?? ""),
  );

  const saveDisabled =
    readOnly || updateRates.isPending || changedRows.length === 0 || hasInvalidDraft;

  function handleReset(row: ProviderRate) {
    setDrafts((prev) => ({
      ...prev,
      [rateKey(row.provider, row.metric)]: microsToDollars(row.default_unit_cost_micros).toFixed(
        4,
      ),
    }));
  }

  function handleSave() {
    updateRates.mutate(changedRows);
  }

  function handleRecalculate() {
    rollupDay.mutate(todayUTC());
  }

  // Focus the drawer on open and restore focus to whatever had it beforehand on close -
  // this is a modal overlay (aria-modal="true" below), so focus must not stay "behind" it
  // on the page underneath.
  React.useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    closeButtonRef.current?.focus();
    return () => {
      previouslyFocused?.focus();
    };
  }, []);

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const sortedRows = [...rows].sort(
    (a, b) => a.provider.localeCompare(b.provider) || a.metric.localeCompare(b.metric),
  );

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" aria-hidden="true" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Provider rates"
        className="fixed right-0 top-0 z-50 h-full w-full max-w-3xl overflow-y-auto rounded-l-xl border-l border-border bg-background p-6 text-foreground"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-foreground">Provider rates</h2>
            <p className="text-sm text-muted-foreground">
              Unit costs in dollars per message, minute, or number action.
            </p>
          </div>
          <Button
            ref={closeButtonRef}
            type="button"
            variant="outline"
            size="sm"
            onClick={onClose}
            className="bg-transparent px-3 py-1.5 text-xs text-foreground/85"
          >
            Close
          </Button>
        </div>

        {readOnly && (
          <p className="mt-3 rounded-lg border border-[hsl(var(--cx-flag)/0.35)] bg-[hsl(var(--cx-flag)/0.1)] px-3 py-2.5 text-sm text-[hsl(var(--cx-flag))]">
            Read-only: rate writes require settings:write.
          </p>
        )}

        {ratesQuery.isLoading ? (
          <p className="mt-4 text-sm text-muted-foreground">Loading rates…</p>
        ) : ratesQuery.isError ? (
          <p role="alert" className="mt-4 text-sm text-destructive">
            {getErrorMessage(ratesQuery.error)}
          </p>
        ) : (
          <table className="mt-4 w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2.5 font-medium">Provider</th>
                <th className="px-3 py-2.5 font-medium">Metric</th>
                <th className="px-3 py-2.5 font-medium">Rate ($/unit)</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((row) => {
                const key = rateKey(row.provider, row.metric);
                const value = drafts[key] ?? "";
                const invalid = isDraftInvalid(value);
                const hintId = `${key}-hint`;
                return (
                  <tr key={key} className="border-b border-border">
                    <td className="px-3 py-2.5 text-foreground/95">{row.provider}</td>
                    <td className="px-3 py-2.5 text-foreground/85">{row.metric}</td>
                    <td className="px-3 py-2.5">
                      <input
                        aria-label={`${row.provider} ${row.metric} rate`}
                        aria-invalid={invalid}
                        aria-describedby={invalid ? hintId : undefined}
                        value={value}
                        onChange={(e) =>
                          setDrafts((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        disabled={readOnly || updateRates.isPending}
                        inputMode="decimal"
                        step="0.0001"
                        className="h-9 w-28 rounded-md border border-border bg-background px-2 text-sm text-foreground aria-[invalid=true]:border-[hsl(var(--cx-danger))]"
                      />
                      {invalid && (
                        <p id={hintId} className="mt-1 text-[10px] text-destructive">
                          Enter a rate ≥ 0
                        </p>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <span
                        className={
                          row.is_override
                            ? "rounded-full bg-[hsl(var(--cx-accent)/0.15)] px-2.5 py-0.5 text-xs text-[hsl(var(--cx-accent))]"
                            : "rounded-full bg-muted px-2.5 py-0.5 text-xs text-muted-foreground"
                        }
                      >
                        {row.is_override ? "override" : "default"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={`Reset ${row.provider} ${row.metric} to default`}
                        onClick={() => handleReset(row)}
                        disabled={readOnly || updateRates.isPending}
                        className="h-auto p-0 text-xs font-normal text-muted-foreground underline hover:bg-transparent hover:text-foreground/95"
                      >
                        Reset
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            type="button"
            size="sm"
            onClick={handleSave}
            disabled={saveDisabled}
            className="px-3 py-1.5 text-sm font-medium"
          >
            Save rates
          </Button>
          <DrawerMutationStatus mutation={updateRates} />
        </div>

        <p className="mt-3 text-xs text-muted-foreground/75">
          Reset sends the default rate and keeps it as an override.
        </p>

        <div className="mt-5 border-t border-border pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleRecalculate}
              disabled={readOnly || rollupDay.isPending}
              className="bg-transparent px-3 py-1.5 text-xs text-foreground/85"
            >
              Recalculate today
            </Button>
            <DrawerMutationStatus
              mutation={rollupDay}
              pendingLabel="Recalculating…"
              successLabel="Recalculated"
            />
          </div>
          <p className="mt-2 text-xs text-muted-foreground/75">
            New rates apply from the next hourly rollup; recalculate to apply now.
          </p>
        </div>
      </aside>
    </>
  );
}
