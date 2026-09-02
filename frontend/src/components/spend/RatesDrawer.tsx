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
      <span className="flex items-center gap-1 text-[10px] text-neutral-500">
        <Loader2 className="h-3 w-3 animate-spin" />
        {pendingLabel}
      </span>
    );
  }
  if (mutation.isError) {
    return (
      <span role="alert" className="text-[10px] text-red-400">
        {getErrorMessage(mutation.error)}
      </span>
    );
  }
  if (mutation.isSuccess) {
    return <span className="text-[10px] text-green-400">{successLabel}</span>;
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
      <div className="fixed inset-0 z-40 bg-black/60" aria-hidden="true" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Provider rates"
        className="fixed right-0 top-0 z-50 h-full w-full max-w-3xl overflow-y-auto border-l border-neutral-800 bg-neutral-950 p-6 text-neutral-100"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-neutral-50">Provider rates</h2>
            <p className="text-sm text-neutral-400">
              Unit costs in dollars per message, minute, or number action.
            </p>
          </div>
          <Button
            ref={closeButtonRef}
            type="button"
            variant="outline"
            size="sm"
            onClick={onClose}
            className="border-neutral-700 bg-transparent px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800"
          >
            Close
          </Button>
        </div>

        {readOnly && (
          <p className="mt-3 text-sm text-amber-400">
            Read-only: rate writes require settings:write.
          </p>
        )}

        {ratesQuery.isLoading ? (
          <p className="mt-4 text-sm text-neutral-400">Loading rates…</p>
        ) : ratesQuery.isError ? (
          <p role="alert" className="mt-4 text-sm text-red-400">
            {getErrorMessage(ratesQuery.error)}
          </p>
        ) : (
          <table className="mt-4 w-full text-sm">
            <thead>
              <tr className="border-b border-neutral-800 text-left text-xs text-neutral-400">
                <th className="px-2 py-2 font-medium">Provider</th>
                <th className="px-2 py-2 font-medium">Metric</th>
                <th className="px-2 py-2 font-medium">Rate ($/unit)</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((row) => {
                const key = rateKey(row.provider, row.metric);
                const value = drafts[key] ?? "";
                const invalid = isDraftInvalid(value);
                const hintId = `${key}-hint`;
                return (
                  <tr key={key} className="border-b border-neutral-800">
                    <td className="px-2 py-2 text-neutral-200">{row.provider}</td>
                    <td className="px-2 py-2 text-neutral-300">{row.metric}</td>
                    <td className="px-2 py-2">
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
                        className="h-9 w-28 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 aria-[invalid=true]:border-red-800"
                      />
                      {invalid && (
                        <p id={hintId} className="mt-1 text-[10px] text-red-400">
                          Enter a rate ≥ 0
                        </p>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <span
                        className={
                          row.is_override
                            ? "rounded-full bg-blue-950 px-2 py-0.5 text-xs text-blue-400"
                            : "rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400"
                        }
                      >
                        {row.is_override ? "override" : "default"}
                      </span>
                    </td>
                    <td className="px-2 py-2">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={`Reset ${row.provider} ${row.metric} to default`}
                        onClick={() => handleReset(row)}
                        disabled={readOnly || updateRates.isPending}
                        className="h-auto p-0 text-xs font-normal text-neutral-400 underline hover:bg-transparent hover:text-neutral-200"
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
            className="bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:opacity-90"
          >
            Save rates
          </Button>
          <DrawerMutationStatus mutation={updateRates} />
        </div>

        <p className="mt-3 text-xs text-neutral-500">
          Reset sends the default rate and keeps it as an override.
        </p>

        <div className="mt-5 border-t border-neutral-800 pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleRecalculate}
              disabled={readOnly || rollupDay.isPending}
              className="border-neutral-700 bg-transparent px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800"
            >
              Recalculate today
            </Button>
            <DrawerMutationStatus
              mutation={rollupDay}
              pendingLabel="Recalculating…"
              successLabel="Recalculated"
            />
          </div>
          <p className="mt-2 text-xs text-neutral-500">
            New rates apply from the next hourly rollup; recalculate to apply now.
          </p>
        </div>
      </aside>
    </>
  );
}
