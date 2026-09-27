import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { formatMicros } from "@/api/spend";
import { Button, Input, Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";

/** Fixed monthly overhead - the things we pay for whether or not any workspace uses them
 * (VPS, storage, monitoring...). The server pro-rates each cost per day into the P&L totals
 * and never charges it to an org. Writes need an admin operator; a reviewer gets a 403 that
 * we surface verbatim rather than pretending the change stuck. */

export type FixedCost = {
  id: string;
  name: string;
  monthly_micros: number;
  starts_on: string;
  ends_on: string | null;
  note: string | null;
  updated_at: string | null;
};

type ConsoleApi = ReturnType<typeof useAuth>["api"];

const FIXED_COSTS_PATH = "/api/v1/ops/console/fixed-costs";
const MICROS_PER_DOLLAR = 1_000_000;

/** Root of the query keys used by the console hooks in @/api/opsConsole (useConsoleOrgs et al).
 * Invalidating it refetches the fixed-cost list *and* the KPI totals rendered by ConsoleTab. */
const CONSOLE_QUERY_ROOT = ["ops", "console"] as const;

export const fixedCostsQueryKey = [...CONSOLE_QUERY_ROOT, "fixed-costs"] as const;

type FixedCostPayload = {
  name: string;
  monthly_micros: number;
  starts_on: string;
  ends_on: string | null;
  note: string | null;
};

type FormState = {
  id: string | null;
  name: string;
  dollars: string;
  startsOn: string;
  endsOn: string;
  note: string;
};

function todayISODate(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function emptyForm(): FormState {
  return { id: null, name: "", dollars: "", startsOn: todayISODate(), endsOn: "", note: "" };
}

function dollarsToMicros(dollars: string): number | null {
  const trimmed = dollars.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.round(value * MICROS_PER_DOLLAR);
}

function microsToDollarsInput(micros: number): string {
  return (micros / MICROS_PER_DOLLAR).toFixed(2);
}

function isOngoingToday(cost: FixedCost, today: string): boolean {
  if (cost.starts_on > today) return false;
  if (cost.ends_on != null && cost.ends_on < today) return false;
  return true;
}

async function fetchFixedCosts(api: ConsoleApi): Promise<FixedCost[]> {
  const data = await api.request<{ fixed_costs: FixedCost[] }>(FIXED_COSTS_PATH);
  return data.fixed_costs ?? [];
}

async function createFixedCost(api: ConsoleApi, payload: FixedCostPayload): Promise<FixedCost> {
  return api.request<FixedCost>(FIXED_COSTS_PATH, { method: "POST", json: payload });
}

async function updateFixedCost(
  api: ConsoleApi,
  id: string,
  payload: FixedCostPayload,
): Promise<FixedCost> {
  return api.request<FixedCost>(`${FIXED_COSTS_PATH}/${id}`, { method: "PUT", json: payload });
}

async function deleteFixedCost(api: ConsoleApi, id: string): Promise<void> {
  await api.request<void>(`${FIXED_COSTS_PATH}/${id}`, { method: "DELETE" });
}

export function FixedCostsSection(): JSX.Element {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const costsQuery = useQuery({
    queryKey: fixedCostsQueryKey,
    queryFn: () => fetchFixedCosts(api),
  });

  const [form, setForm] = React.useState<FormState>(() => emptyForm());
  const [formOpen, setFormOpen] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = React.useState<string | null>(null);

  const saveMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string | null; payload: FixedCostPayload }) =>
      id == null ? createFixedCost(api, payload) : updateFixedCost(api, id, payload),
    onSuccess: () => {
      setForm(emptyForm());
      setFormError(null);
      setFormOpen(false);
      void queryClient.invalidateQueries({ queryKey: CONSOLE_QUERY_ROOT });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteFixedCost(api, id),
    onSuccess: () => {
      setConfirmingDeleteId(null);
      void queryClient.invalidateQueries({ queryKey: CONSOLE_QUERY_ROOT });
    },
  });

  const costs = costsQuery.data ?? [];

  const ongoingTotalMicros = React.useMemo(() => {
    const today = todayISODate();
    return costs.reduce(
      (sum, cost) => (isOngoingToday(cost, today) ? sum + cost.monthly_micros : sum),
      0,
    );
  }, [costs]);

  function startAdd() {
    setForm(emptyForm());
    setFormError(null);
    saveMutation.reset();
    setFormOpen(true);
  }

  function startEdit(cost: FixedCost) {
    setForm({
      id: cost.id,
      name: cost.name,
      dollars: microsToDollarsInput(cost.monthly_micros),
      startsOn: cost.starts_on,
      endsOn: cost.ends_on ?? "",
      note: cost.note ?? "",
    });
    setFormError(null);
    saveMutation.reset();
    setFormOpen(true);
  }

  function cancelForm() {
    setForm(emptyForm());
    setFormError(null);
    saveMutation.reset();
    setFormOpen(false);
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();

    const name = form.name.trim();
    if (name === "") {
      setFormError("Name is required.");
      return;
    }

    const monthlyMicros = dollarsToMicros(form.dollars);
    if (monthlyMicros === null) {
      setFormError("Per month must be a dollar amount of 0 or more.");
      return;
    }

    const startsOn = form.startsOn.trim() === "" ? todayISODate() : form.startsOn.trim();
    const endsOn = form.endsOn.trim() === "" ? null : form.endsOn.trim();
    if (endsOn !== null && endsOn < startsOn) {
      setFormError("Until must be on or after From.");
      return;
    }

    setFormError(null);
    saveMutation.mutate({
      id: form.id,
      payload: {
        name,
        monthly_micros: monthlyMicros,
        starts_on: startsOn,
        ends_on: endsOn,
        note: form.note.trim() === "" ? null : form.note.trim(),
      },
    });
  }

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Fixed monthly costs</SectionLabel>
      <p className="text-xs text-muted-foreground">
        Overhead we pay whoever uses the platform. Pro-rated per day into the P&L totals, never
        onto a workspace.
      </p>

      {costsQuery.isPending ? (
        <Spinner label="Loading fixed costs" />
      ) : costsQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(costsQuery.error)}
        </p>
      ) : costs.length > 0 ? (
        <div className="overflow-x-auto rounded-[var(--cx-r-md,14px)] border border-border">
          <table className="w-full min-w-[720px] border-collapse text-[13px]">
            <thead>
              <tr className="bg-muted text-muted-foreground">
                <th className="px-3 py-2 text-left font-semibold">Name</th>
                <th className="px-3 py-2 text-right font-semibold">Per month</th>
                <th className="px-3 py-2 text-left font-semibold">From</th>
                <th className="px-3 py-2 text-left font-semibold">Until</th>
                <th className="px-3 py-2 text-left font-semibold">Note</th>
                <th className="px-3 py-2 text-right font-semibold" />
              </tr>
            </thead>
            <tbody>
              {costs.map((item) => (
                <tr key={item.id} className="border-t border-border">
                  <td className="px-3 py-2 text-left font-medium">{item.name}</td>
                  <td className="px-3 py-2 text-right">{formatMicros(item.monthly_micros)}</td>
                  <td className="px-3 py-2 text-left">{item.starts_on}</td>
                  <td className="px-3 py-2 text-left">{item.ends_on ?? "Ongoing"}</td>
                  <td className="px-3 py-2 text-left">{item.note ?? "—"}</td>
                  <td className="px-3 py-2 text-right">
                    <div className="flex justify-end gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => startEdit(item)}
                      >
                        Edit
                      </Button>
                      {confirmingDeleteId === item.id ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={deleteMutation.isPending}
                          onClick={() => deleteMutation.mutate(item.id)}
                        >
                          Confirm delete
                        </Button>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => setConfirmingDeleteId(item.id)}
                        >
                          Delete
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <ConsoleEmpty>No fixed costs yet.</ConsoleEmpty>
      )}

      {deleteMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(deleteMutation.error)}
        </p>
      ) : null}

      {formOpen ? (
        <form className="space-y-2" onSubmit={handleSubmit}>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            <Input
              aria-label="Name"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            />
            <Input
              aria-label="Per month in dollars"
              type="number"
              inputMode="decimal"
              min="0"
              step="0.01"
              placeholder="0.00"
              value={form.dollars}
              onChange={(e) => setForm((prev) => ({ ...prev, dollars: e.target.value }))}
            />
            <Input
              aria-label="From"
              type="date"
              value={form.startsOn}
              onChange={(e) => setForm((prev) => ({ ...prev, startsOn: e.target.value }))}
            />
            <Input
              aria-label="Until"
              type="date"
              value={form.endsOn}
              onChange={(e) => setForm((prev) => ({ ...prev, endsOn: e.target.value }))}
            />
            <Input
              aria-label="Note"
              placeholder="Note (optional)"
              value={form.note}
              onChange={(e) => setForm((prev) => ({ ...prev, note: e.target.value }))}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={saveMutation.isPending}>
              Save
            </Button>
            <Button type="button" variant="ghost" onClick={cancelForm}>
              Cancel
            </Button>
          </div>
          {formError ? (
            <p role="alert" className="text-sm text-destructive">
              {formError}
            </p>
          ) : null}
          {saveMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {mutationErrorMessage(saveMutation.error)}
            </p>
          ) : null}
        </form>
      ) : (
        <Button type="button" variant="outline" onClick={startAdd}>
          Add cost
        </Button>
      )}

      {costsQuery.isSuccess ? (
        <p className="text-xs text-muted-foreground">
          Total per month: {formatMicros(ongoingTotalMicros)}
        </p>
      ) : null}
    </SurfaceCard>
  );
}
