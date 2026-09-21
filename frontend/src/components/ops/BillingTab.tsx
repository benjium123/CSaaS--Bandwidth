import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { formatCredits } from "@/api/billing";
import { dollarsToMicros, lastNDaysRange } from "@/api/spend";
import { AccountsTab } from "@/components/ops/AccountsTab";
import {
  Button,
  Input,
  Pill,
  Select,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import {
  ConsoleCard,
  ConsoleEmpty,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

/** P45: the operator's billing view. Pick a workspace (or paste an id), see its credit
 * balance, reserved and available figures and its prepaid state, hand-post a manual
 * adjustment, and check recent AI margin.
 *
 * The backend read does NOT echo the org id or name back, so the panel heading uses
 * whatever the picker handed us (`selected.name`). */

type SelectedOrg = { orgId: string; name: string };

export type OrgBilling = {
  balance_micros: number;
  reserved_micros: number;
  ai_markup_bps: number;
  ai_platform_fee_per_minute_micros: number | null;
  ai_key_mode: string;
  telephony_prepaid: boolean;
  telephony_prepaid_since: string | null;
};

export type AdjustmentResult = {
  id: string;
  amount_micros: number;
  balance_after_micros: number;
};

/** `GET /api/v1/platform/billing/margin` returns a BARE array, not `{ items }`. */
export type MarginDay = {
  day: string;
  cost_micros: number;
  price_micros: number;
  margin_micros: number;
};

/**
 * Real money: the sign comes from the Direction select, never from a minus typed into the
 * amount box, so an amount is only valid when it parses strictly positive. `dollarsToMicros`
 * rounds, so we always send an integer cent-micros value.
 */
function AdjustmentForm({
  orgId,
  name,
}: {
  orgId: string;
  name: string;
}): JSX.Element {
  const { api } = useAuth();
  const qc = useQueryClient();

  const [direction, setDirection] = React.useState<"credit" | "debit">("credit");
  const [entryType, setEntryType] = React.useState<"adjustment" | "refund">(
    "adjustment",
  );
  const [amount, setAmount] = React.useState("");
  const [note, setNote] = React.useState("");

  const parsed = Number(amount);
  const amountValid =
    amount.trim() !== "" && Number.isFinite(parsed) && parsed > 0;
  const sign = direction === "debit" ? -1 : 1;
  const amountMicros = amountValid ? dollarsToMicros(parsed) * sign : 0;

  const adjust = useMutation({
    mutationFn: () =>
      api.request<AdjustmentResult>(
        `/api/v1/platform/billing/orgs/${orgId}/adjustments`,
        {
          method: "POST",
          json: {
            amount_micros: amountMicros,
            note: note.trim(),
            entry_type: entryType,
          },
        },
      ),
    onSuccess: () => {
      setAmount("");
      setNote("");
      void qc.invalidateQueries({ queryKey: ["ops", "billing"] });
    },
  });

  const previewText =
    `You are about to ${direction === "debit" ? "DEBIT" : "CREDIT"} ` +
    `${formatCredits(Math.abs(amountMicros))} ` +
    `${direction === "debit" ? "from" : "to"} ${name}.`;

  return (
    <ConsoleCard className="space-y-[11px]">
      <SectionLabel>Manual adjustment</SectionLabel>
      <div className="grid gap-[11px] sm:grid-cols-2">
        <Select
          aria-label="Direction"
          value={direction}
          onChange={(e) => setDirection(e.target.value as "credit" | "debit")}
        >
          <option value="credit">Credit - add money</option>
          <option value="debit">Debit - take money away</option>
        </Select>
        <Select
          aria-label="Entry type"
          value={entryType}
          onChange={(e) =>
            setEntryType(e.target.value as "adjustment" | "refund")
          }
        >
          <option value="adjustment">adjustment</option>
          <option value="refund">refund</option>
        </Select>
      </div>
      <Input
        aria-label="Amount in dollars"
        inputMode="decimal"
        placeholder="0.00"
        value={amount}
        onChange={(e) => setAmount(e.target.value)}
      />
      <Textarea
        aria-label="Note"
        rows={2}
        placeholder="What you checked and why"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      {amountValid ? (
        <p
          data-testid="ops-adjustment-preview"
          className="text-[13.5px] text-[hsl(var(--cx-text))]"
        >
          {previewText}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-[11px]">
        <Button
          type="button"
          disabled={!amountValid || note.trim() === "" || adjust.isPending}
          onClick={() => adjust.mutate()}
        >
          Apply adjustment
        </Button>
      </div>
      {adjust.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(adjust.error)}
        </p>
      ) : null}
      {adjust.isSuccess && adjust.data ? (
        <p role="status" className="text-sm text-[hsl(var(--cx-text))]">
          New balance {formatCredits(adjust.data.balance_after_micros)}
        </p>
      ) : null}
    </ConsoleCard>
  );
}

function MarginSection({ orgId }: { orgId: string }): JSX.Element {
  const { api } = useAuth();
  const range = React.useMemo(() => lastNDaysRange(30), []);

  const q = useQuery({
    queryKey: ["ops", "billing", "margin", range.from, range.to, orgId],
    queryFn: () => {
      const search = new URLSearchParams({
        from: range.from,
        to: range.to,
        org_id: orgId,
      });
      return api.request<MarginDay[]>(
        `/api/v1/platform/billing/margin?${search.toString()}`,
      );
    },
    retry: false,
  });

  // A surprise object (rather than the bare array) must not crash the tab.
  const rows = Array.isArray(q.data) ? q.data : [];
  const totals = rows.reduce(
    (acc, r) => ({
      price: acc.price + r.price_micros,
      cost: acc.cost + r.cost_micros,
      margin: acc.margin + r.margin_micros,
    }),
    { price: 0, cost: 0, margin: 0 },
  );

  return (
    <SurfaceCard className="space-y-[11px]">
      <SectionLabel>Margin</SectionLabel>
      {q.isPending ? (
        <Spinner label="Loading margin" />
      ) : q.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(q.error)}
        </p>
      ) : rows.length === 0 ? (
        <ConsoleEmpty>No AI usage in this window.</ConsoleEmpty>
      ) : (
        <div className="overflow-hidden rounded-[var(--cx-r-md,14px)] border border-[hsl(var(--cx-line))]">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="bg-[hsl(var(--cx-overlay))] text-[hsl(var(--cx-muted))]">
                <th className="px-[11px] py-[9px] text-left font-semibold">Day</th>
                <th className="px-[11px] py-[9px] text-right font-semibold">Price</th>
                <th className="px-[11px] py-[9px] text-right font-semibold">Cost</th>
                <th className="px-[11px] py-[9px] text-right font-semibold">Margin</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.day}
                  className="border-t border-[hsl(var(--cx-line))] text-[hsl(var(--cx-text))]"
                >
                  <td className="px-[11px] py-[9px] text-left">{row.day}</td>
                  <td className="px-[11px] py-[9px] text-right">
                    {formatCredits(row.price_micros)}
                  </td>
                  <td className="px-[11px] py-[9px] text-right">
                    {formatCredits(row.cost_micros)}
                  </td>
                  <td className="px-[11px] py-[9px] text-right">
                    {formatCredits(row.margin_micros)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-[hsl(var(--cx-line))] font-semibold text-[hsl(var(--cx-text))]">
                <td className="px-[11px] py-[9px] text-left">Total</td>
                <td className="px-[11px] py-[9px] text-right">
                  {formatCredits(totals.price)}
                </td>
                <td className="px-[11px] py-[9px] text-right">
                  {formatCredits(totals.cost)}
                </td>
                <td className="px-[11px] py-[9px] text-right">
                  {formatCredits(totals.margin)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </SurfaceCard>
  );
}

function BillingPanel({
  selected,
  onBack,
}: {
  selected: SelectedOrg;
  onBack: () => void;
}): JSX.Element {
  const { api } = useAuth();
  const qc = useQueryClient();

  const billing = useQuery({
    queryKey: ["ops", "billing", "org", selected.orgId],
    queryFn: () =>
      api.request<OrgBilling>(`/api/v1/platform/billing/orgs/${selected.orgId}`),
    retry: false,
  });

  const togglePrepaid = useMutation({
    mutationFn: (next: boolean) =>
      api.request<OrgBilling>(`/api/v1/platform/billing/orgs/${selected.orgId}`, {
        method: "PATCH",
        json: { telephony_prepaid: next },
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["ops", "billing"] }),
  });

  const data = billing.data;

  return (
    <div className="space-y-[14px]">
      <Button type="button" variant="ghost" onClick={onBack}>
        ← Back to workspaces
      </Button>

      {billing.isPending ? (
        <Spinner label="Loading billing" />
      ) : billing.isError || !data ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(billing.error)}
        </p>
      ) : (
        <SurfaceCard className="space-y-[14px]">
          <div className="flex flex-wrap items-center gap-[11px]">
            <h2 className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
              {selected.name}
            </h2>
            <Pill tone={data.telephony_prepaid ? "success" : "neutral"}>
              {data.telephony_prepaid ? "Prepaid on" : "Prepaid off"}
            </Pill>
          </div>

          {data.telephony_prepaid && data.telephony_prepaid_since ? (
            <p className="text-xs text-[hsl(var(--cx-muted))]">
              Since {new Date(data.telephony_prepaid_since).toLocaleDateString()}
            </p>
          ) : null}

          <div className="grid gap-[14px] sm:grid-cols-3">
            <div className="space-y-[3px]">
              <SectionLabel>Credit balance</SectionLabel>
              <p
                data-testid="ops-billing-balance"
                className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]"
              >
                {formatCredits(data.balance_micros)}
              </p>
            </div>
            <div className="space-y-[3px]">
              <SectionLabel>Reserved</SectionLabel>
              <p className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
                {formatCredits(data.reserved_micros)}
              </p>
            </div>
            <div className="space-y-[3px]">
              <SectionLabel>Available</SectionLabel>
              <p className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
                {formatCredits(data.balance_micros - data.reserved_micros)}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-[11px]">
            <SectionLabel>AI markup</SectionLabel>
            <span className="text-[13.5px] text-[hsl(var(--cx-text))]">
              {(data.ai_markup_bps / 100).toFixed(2)}%
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-[11px]">
            <Button
              type="button"
              variant="outline"
              disabled={togglePrepaid.isPending}
              onClick={() => togglePrepaid.mutate(!data.telephony_prepaid)}
            >
              {data.telephony_prepaid ? "Turn prepaid off" : "Turn prepaid on"}
            </Button>
          </div>

          {togglePrepaid.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {mutationErrorMessage(togglePrepaid.error)}
            </p>
          ) : null}
        </SurfaceCard>
      )}

      <AdjustmentForm orgId={selected.orgId} name={selected.name} />
      <MarginSection orgId={selected.orgId} />
    </div>
  );
}

export function BillingTab(): JSX.Element {
  const [selected, setSelected] = React.useState<SelectedOrg | null>(null);
  const [rawId, setRawId] = React.useState("");

  if (selected !== null) {
    return <BillingPanel selected={selected} onBack={() => setSelected(null)} />;
  }

  return (
    <div className="space-y-[14px]">
      <SurfaceCard className="space-y-[11px]">
        <SectionLabel>Open a workspace by id</SectionLabel>
        <form
          className="flex flex-wrap items-center gap-[11px]"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = rawId.trim();
            if (!trimmed) return;
            setSelected({ orgId: trimmed, name: trimmed });
          }}
        >
          <Input
            aria-label="Workspace id"
            placeholder="Or paste a workspace id"
            value={rawId}
            onChange={(e) => setRawId(e.target.value)}
            className="w-full max-w-[320px]"
          />
          <Button type="submit" variant="outline">
            Open
          </Button>
        </form>
      </SurfaceCard>

      <NumberPurchases />
      <SectionLabel>Pick a workspace</SectionLabel>
      <AccountsTab
        onPickOrg={(account) =>
          setSelected({ orgId: account.org_id, name: account.name })
        }
      />
    </div>
  );
}

function NumberPurchases() {
  const { api } = useAuth();
  const query = useQuery({ queryKey: ["ops", "number-purchases"], queryFn: () => api.request<{ id: string; org_id: string; state: string; detail?: string; subscription_id?: string; monthly_total_cents: number }[]>("/api/v1/ops/number-purchases") });
  return <SurfaceCard className="space-y-4"><h2 className="text-lg font-semibold">Phone number purchases</h2>
    {query.isError && <p role="alert">{mutationErrorMessage(query.error)}</p>}
    {query.data?.length === 0 && <p className="text-sm text-slate-500">No number checkouts yet.</p>}
    {query.data?.map(p => <div key={p.id} className="rounded-xl border p-4"><div className="flex justify-between"><strong>{p.state.replace(/_/g, " ")}</strong><span>${p.monthly_total_cents / 100}/month</span></div><p className="my-2 text-sm">{p.detail}</p><p className="text-xs text-slate-500">Workspace: {p.org_id}<br />Purchase: {p.id}</p>{p.subscription_id && <a className="mt-2 inline-block text-sm text-blue-700 underline" href={`https://dashboard.stripe.com/subscriptions/${encodeURIComponent(p.subscription_id)}`} target="_blank" rel="noreferrer">View subscription in Stripe</a>}</div>)}
  </SurfaceCard>;
}
