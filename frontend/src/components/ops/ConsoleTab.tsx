import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { formatMicros, lastNDaysRange } from "@/api/spend";
import {
  carrierCounts,
  downloadConsoleOrgsCsv,
  priceDollarsStringToMicros,
  priceMicrosToDollarsString,
  priceUnitLabel,
  useAdjustConsoleOrg,
  useConsoleOrg,
  useConsoleOrgs,
  useConsolePayments,
  useConsolePrices,
  useGrantConsoleBundle,
  useSetConsolePrepaid,
  useUpdateConsolePrice,
  type ConsoleMetrics,
  type ConsoleOrgRow,
  type ConsolePrice,
  type DateRange,
} from "@/api/opsConsole";
import {
  Button,
  Drawer,
  Input,
  Pill,
  Select,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import { ConsoleCard, ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";

/** P?: the admin "Console" tab - billing v2's cross-tenant view. Every org's purchases,
 * discounts, usage, traffic, blocked attempts and our profit over a date range, the
 * platform price list, and the manual credit/bundle/prepaid tools. Reviewers get read-only
 * (writes 403 server-side - the mutation error paths below just show that message rather
 * than assuming success). */

function count(n: number): string {
  return n.toLocaleString();
}

type BillingStateTone = "success" | "warning" | "danger";

function stateTone(billingState: string): BillingStateTone {
  if (billingState === "exhausted") return "danger";
  if (billingState === "low") return "warning";
  return "success";
}

// -----------------------------------------------------------------------------------------
// Date range picker + CSV export
// -----------------------------------------------------------------------------------------

function RangePicker({
  range,
  onChange,
}: {
  range: DateRange;
  onChange: (next: DateRange) => void;
}): JSX.Element {
  const { api } = useAuth();
  const [exporting, setExporting] = React.useState(false);
  const [exportError, setExportError] = React.useState<string | null>(null);

  async function handleExport() {
    setExporting(true);
    setExportError(null);
    try {
      await downloadConsoleOrgsCsv(api, range);
    } catch (err) {
      setExportError(mutationErrorMessage(err));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Start
        <Input
          type="date"
          aria-label="Start date"
          value={range.start}
          max={range.end}
          onChange={(e) => onChange({ ...range, start: e.target.value })}
          className="w-auto"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        End
        <Input
          type="date"
          aria-label="End date"
          value={range.end}
          min={range.start}
          onChange={(e) => onChange({ ...range, end: e.target.value })}
          className="w-auto"
        />
      </label>
      <Button type="button" variant="outline" disabled={exporting} onClick={handleExport}>
        {exporting ? "Exporting…" : "Export CSV"}
      </Button>
      {exportError ? (
        <p role="alert" className="text-sm text-destructive">
          {exportError}
        </p>
      ) : null}
    </div>
  );
}

// -----------------------------------------------------------------------------------------
// KPI cards
// -----------------------------------------------------------------------------------------

function KpiCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
}): JSX.Element {
  return (
    <ConsoleCard className="space-y-1">
      <SectionLabel>{label}</SectionLabel>
      <p className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
        {value}
      </p>
      {sub != null ? <p className="text-xs text-muted-foreground">{sub}</p> : null}
    </ConsoleCard>
  );
}

function KpiGrid({
  totals,
  summary,
}: {
  totals: ConsoleMetrics;
  summary: {
    orgs: number;
    low_orgs: number;
    exhausted_orgs: number;
    balances_micros: number;
    sms_bundle_units_outstanding: number;
    mms_bundle_units_outstanding: number;
  };
}): JSX.Element {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <KpiCard label="Cash profit" value={formatMicros(totals.cash_profit ?? 0)} />
      <KpiCard
        label="Paid"
        value={formatMicros(totals.paid ?? 0)}
        sub={`List ${formatMicros(totals.list ?? 0)} · Discounts ${formatMicros(totals.discount ?? 0)}`}
      />
      <KpiCard label="Stripe fees" value={formatMicros(totals.stripe_fees ?? 0)} />
      <KpiCard label="Carrier cost" value={formatMicros(totals.carrier_cost ?? 0)} />
      <KpiCard label="Usage revenue" value={formatMicros(totals.usage_revenue ?? 0)} />
      <KpiCard label="Balances held" value={formatMicros(summary.balances_micros)} />
      <KpiCard
        label="Orgs"
        value={count(summary.orgs)}
        sub={`${count(summary.low_orgs)} low · ${count(summary.exhausted_orgs)} exhausted`}
      />
      <KpiCard
        label="Bundle units outstanding"
        value={`${count(summary.sms_bundle_units_outstanding)} SMS`}
        sub={`${count(summary.mms_bundle_units_outstanding)} MMS`}
      />
    </div>
  );
}

// -----------------------------------------------------------------------------------------
// Traffic totals
// -----------------------------------------------------------------------------------------

function TrafficSection({ totals }: { totals: ConsoleMetrics }): JSX.Element {
  const carriers = carrierCounts(totals);
  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Traffic</SectionLabel>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <KpiCard
          label="Texts (segments)"
          value={`${count(totals.sms_out_segments ?? 0)} out`}
          sub={`${count(totals.sms_in_segments ?? 0)} in`}
        />
        <KpiCard
          label="MMS"
          value={`${count(totals.mms_out ?? 0)} out`}
          sub={`${count(totals.mms_in ?? 0)} in`}
        />
        <KpiCard
          label="Calls"
          value={`${count(totals.calls_out ?? 0)} out (${count(totals.calls_out_answered ?? 0)} answered)`}
          sub={`${count(totals.calls_in ?? 0)} in (${count(totals.calls_in_answered ?? 0)} answered)`}
        />
        <KpiCard
          label="Minutes"
          value={`${count(totals.minutes_out ?? 0)} out`}
          sub={`${count(totals.minutes_in ?? 0)} in`}
        />
        <KpiCard
          label="Blocked"
          value={`${count(totals.blocked_total ?? 0)} total`}
          sub={`Credit ${count(totals.blocked_credit ?? 0)} · Compliance ${count(
            totals.blocked_compliance ?? 0,
          )} · Moderation ${count(totals.blocked_moderation ?? 0)}`}
        />
      </div>
      {carriers.length > 0 ? (
        <div className="overflow-hidden rounded-[var(--cx-r-md,14px)] border border-border">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="bg-muted text-muted-foreground">
                <th className="px-3 py-2 text-left font-semibold">Carrier</th>
                <th className="px-3 py-2 text-right font-semibold">Texts</th>
                <th className="px-3 py-2 text-right font-semibold">Calls</th>
              </tr>
            </thead>
            <tbody>
              {carriers.map((row) => (
                <tr key={row.carrier} className="border-t border-border">
                  <td className="px-3 py-2 text-left">{row.carrier}</td>
                  <td className="px-3 py-2 text-right">{count(row.texts)}</td>
                  <td className="px-3 py-2 text-right">{count(row.calls)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </SurfaceCard>
  );
}

// -----------------------------------------------------------------------------------------
// Orgs table
// -----------------------------------------------------------------------------------------

type SortKey =
  | "name"
  | "billing_state"
  | "prepaid"
  | "balance_micros"
  | "paid"
  | "discount"
  | "usage_revenue"
  | "cash_profit"
  | "sms_out_segments"
  | "sms_in_segments"
  | "minutes_out"
  | "minutes_in"
  | "blocked_total"
  | "auto_recharge";

type SortState = { key: SortKey; direction: "asc" | "desc" } | null;

function sortValue(row: ConsoleOrgRow, key: SortKey): string | number {
  switch (key) {
    case "name":
      return row.name.toLowerCase();
    case "billing_state":
      return row.billing_state;
    case "prepaid":
      return row.prepaid ? 1 : 0;
    case "balance_micros":
      return row.balance_micros;
    case "auto_recharge":
      return row.auto_recharge ? 1 : 0;
    default:
      return row.metrics[key] ?? 0;
  }
}

function sortOrgs(orgs: ConsoleOrgRow[], sort: SortState): ConsoleOrgRow[] {
  if (!sort) return orgs;
  const { key, direction } = sort;
  const sign = direction === "asc" ? 1 : -1;
  return [...orgs].sort((a, b) => {
    const av = sortValue(a, key);
    const bv = sortValue(b, key);
    if (av < bv) return -1 * sign;
    if (av > bv) return 1 * sign;
    return 0;
  });
}

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "billing_state", label: "State" },
  { key: "prepaid", label: "Prepaid" },
  { key: "balance_micros", label: "Balance" },
  { key: "paid", label: "Paid" },
  { key: "discount", label: "Discount" },
  { key: "usage_revenue", label: "Usage revenue" },
  { key: "cash_profit", label: "Cash profit" },
  { key: "sms_out_segments", label: "Texts out" },
  { key: "sms_in_segments", label: "Texts in" },
  { key: "minutes_out", label: "Minutes out" },
  { key: "minutes_in", label: "Minutes in" },
  { key: "blocked_total", label: "Blocked" },
  { key: "auto_recharge", label: "Auto-recharge" },
];

function OrgsTable({
  orgs,
  onOpenOrg,
}: {
  orgs: ConsoleOrgRow[];
  onOpenOrg: (orgId: string) => void;
}): JSX.Element {
  const [sort, setSort] = React.useState<SortState>(null);
  const sorted = React.useMemo(() => sortOrgs(orgs, sort), [orgs, sort]);

  function toggleSort(key: SortKey) {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, direction: "asc" };
      return { key, direction: prev.direction === "asc" ? "desc" : "asc" };
    });
  }

  if (orgs.length === 0) {
    return <ConsoleEmpty>No workspaces in this window.</ConsoleEmpty>;
  }

  return (
    <div className="overflow-x-auto rounded-[var(--cx-r-md,14px)] border border-border">
      <table className="w-full min-w-[1100px] border-collapse text-[13px]">
        <thead>
          <tr className="bg-muted text-muted-foreground">
            {COLUMNS.map((col) => {
              const active = sort?.key === col.key;
              return (
                <th
                  key={col.key}
                  scope="col"
                  aria-sort={active ? (sort!.direction === "asc" ? "ascending" : "descending") : "none"}
                  className="px-3 py-2 text-left font-semibold"
                >
                  <button
                    type="button"
                    onClick={() => toggleSort(col.key)}
                    className="inline-flex items-center gap-1 hover:underline"
                  >
                    {col.label}
                    {active ? (sort!.direction === "asc" ? " ▲" : " ▼") : null}
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.org_id} className="border-t border-border">
              <td className="px-3 py-2 text-left">
                <button
                  type="button"
                  onClick={() => onOpenOrg(row.org_id)}
                  className="font-medium text-[hsl(var(--cx-accent))] hover:underline"
                >
                  {row.name}
                </button>
              </td>
              <td className="px-3 py-2 text-left">
                <Pill tone={stateTone(row.billing_state)}>{row.billing_state}</Pill>
              </td>
              <td className="px-3 py-2 text-left">{row.prepaid ? "Yes" : "No"}</td>
              <td className="px-3 py-2 text-right">{formatMicros(row.balance_micros)}</td>
              <td className="px-3 py-2 text-right">{formatMicros(row.metrics.paid ?? 0)}</td>
              <td className="px-3 py-2 text-right">{formatMicros(row.metrics.discount ?? 0)}</td>
              <td className="px-3 py-2 text-right">{formatMicros(row.metrics.usage_revenue ?? 0)}</td>
              <td className="px-3 py-2 text-right">{formatMicros(row.metrics.cash_profit ?? 0)}</td>
              <td className="px-3 py-2 text-right">{count(row.metrics.sms_out_segments ?? 0)}</td>
              <td className="px-3 py-2 text-right">{count(row.metrics.sms_in_segments ?? 0)}</td>
              <td className="px-3 py-2 text-right">{count(row.metrics.minutes_out ?? 0)}</td>
              <td className="px-3 py-2 text-right">{count(row.metrics.minutes_in ?? 0)}</td>
              <td className="px-3 py-2 text-right">{count(row.metrics.blocked_total ?? 0)}</td>
              <td className="px-3 py-2 text-left">{row.auto_recharge ? "On" : "Off"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// -----------------------------------------------------------------------------------------
// Org detail drawer - admin actions
// -----------------------------------------------------------------------------------------

function AdjustmentForm({ orgId }: { orgId: string }): JSX.Element {
  const { api } = useAuth();
  const adjust = useAdjustConsoleOrg(api);
  const [direction, setDirection] = React.useState<"credit" | "debit">("credit");
  const [amount, setAmount] = React.useState("");
  const [note, setNote] = React.useState("");

  const parsed = Number(amount);
  const amountValid = amount.trim() !== "" && Number.isFinite(parsed) && parsed > 0;
  const noteValid = note.trim().length >= 3;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!amountValid || !noteValid) return;
    const sign = direction === "debit" ? -1 : 1;
    const amountMicros = Math.round(parsed * 1_000_000) * sign;
    adjust.mutate(
      { orgId, amountMicros, note: note.trim() },
      {
        onSuccess: () => {
          setAmount("");
          setNote("");
        },
      },
    );
  }

  return (
    <form className="space-y-2" onSubmit={handleSubmit}>
      <SectionLabel>Credit adjustment</SectionLabel>
      <div className="grid gap-2 sm:grid-cols-2">
        <Select
          aria-label="Adjustment direction"
          value={direction}
          onChange={(e) => setDirection(e.target.value as "credit" | "debit")}
        >
          <option value="credit">Credit - add money</option>
          <option value="debit">Debit - take money away</option>
        </Select>
        <Input
          aria-label="Adjustment amount in dollars"
          inputMode="decimal"
          placeholder="0.00"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
        />
      </div>
      <Textarea
        aria-label="Adjustment note"
        rows={2}
        placeholder="Reason (required)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <Button type="submit" disabled={!amountValid || !noteValid || adjust.isPending}>
        Apply adjustment
      </Button>
      {adjust.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(adjust.error)}
        </p>
      ) : null}
      {adjust.isSuccess ? (
        <p role="status" className="text-sm text-muted-foreground">
          New balance {formatMicros(adjust.data.balance_after_micros)}
        </p>
      ) : null}
    </form>
  );
}

function BundleForm({ orgId }: { orgId: string }): JSX.Element {
  const { api } = useAuth();
  const grant = useGrantConsoleBundle(api);
  const [kind, setKind] = React.useState<"sms" | "mms">("sms");
  const [units, setUnits] = React.useState("");
  const [note, setNote] = React.useState("");

  const parsedUnits = Number(units);
  const unitsValid = units.trim() !== "" && Number.isInteger(parsedUnits) && parsedUnits > 0;
  const noteValid = note.trim().length >= 3;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!unitsValid || !noteValid) return;
    grant.mutate(
      { orgId, kind, units: parsedUnits, note: note.trim() },
      {
        onSuccess: () => {
          setUnits("");
          setNote("");
        },
      },
    );
  }

  return (
    <form className="space-y-2" onSubmit={handleSubmit}>
      <SectionLabel>Grant bundle units</SectionLabel>
      <div className="grid gap-2 sm:grid-cols-2">
        <Select aria-label="Bundle kind" value={kind} onChange={(e) => setKind(e.target.value as "sms" | "mms")}>
          <option value="sms">SMS</option>
          <option value="mms">MMS</option>
        </Select>
        <Input
          aria-label="Bundle units"
          inputMode="numeric"
          placeholder="Units"
          value={units}
          onChange={(e) => setUnits(e.target.value)}
        />
      </div>
      <Textarea
        aria-label="Bundle grant note"
        rows={2}
        placeholder="Reason (required)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <Button type="submit" disabled={!unitsValid || !noteValid || grant.isPending}>
        Grant units
      </Button>
      {grant.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(grant.error)}
        </p>
      ) : null}
      {grant.isSuccess ? (
        <p role="status" className="text-sm text-muted-foreground">
          Units after grant: {count(grant.data.units_after)}
        </p>
      ) : null}
    </form>
  );
}

function PrepaidToggle({ orgId, enabled }: { orgId: string; enabled: boolean }): JSX.Element {
  const { api } = useAuth();
  const setPrepaid = useSetConsolePrepaid(api);
  return (
    <div className="space-y-2">
      <SectionLabel>Prepaid</SectionLabel>
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={enabled ? "success" : "neutral"}>{enabled ? "Prepaid on" : "Prepaid off"}</Pill>
        <Button
          type="button"
          variant="outline"
          disabled={setPrepaid.isPending}
          onClick={() => setPrepaid.mutate({ orgId, enabled: !enabled })}
        >
          {enabled ? "Turn prepaid off" : "Turn prepaid on"}
        </Button>
      </div>
      {setPrepaid.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(setPrepaid.error)}
        </p>
      ) : null}
    </div>
  );
}

function OrgDetailBody({ orgId, range }: { orgId: string; range: DateRange }): JSX.Element {
  const { api } = useAuth();
  const detail = useConsoleOrg(api, orgId, range);

  if (detail.isPending) return <Spinner label="Loading workspace" />;
  if (detail.isError || !detail.data) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {mutationErrorMessage(detail.error)}
      </p>
    );
  }

  const { org, metrics, series, ledger, payments, refusals, numbers } = detail.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={stateTone(org.billing_state)}>{org.billing_state}</Pill>
        <Pill tone={org.prepaid ? "success" : "neutral"}>{org.prepaid ? "Prepaid" : "Not prepaid"}</Pill>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <KpiCard label="Balance" value={formatMicros(org.balance_micros)} />
        <KpiCard label="Cash profit" value={formatMicros(metrics.cash_profit ?? 0)} />
        <KpiCard label="Usage revenue" value={formatMicros(metrics.usage_revenue ?? 0)} />
        <KpiCard label="Paid" value={formatMicros(metrics.paid ?? 0)} />
        <KpiCard label="Blocked" value={count(metrics.blocked_total ?? 0)} />
        <KpiCard
          label="Bundle units"
          value={`${count(org.sms_bundle_units)} SMS`}
          sub={`${count(org.mms_bundle_units)} MMS`}
        />
      </div>

      <div>
        <SectionLabel>Daily</SectionLabel>
        {series.length === 0 ? (
          <ConsoleEmpty>No activity in this window.</ConsoleEmpty>
        ) : (
          <div className="mt-2 max-h-64 overflow-auto rounded-[var(--cx-r-md,14px)] border border-border">
            <table className="w-full min-w-[720px] border-collapse text-[12.5px]">
              <thead>
                <tr className="bg-muted text-muted-foreground">
                  <th className="px-2 py-1.5 text-left font-semibold">Date</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Texts out</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Texts in</th>
                  <th className="px-2 py-1.5 text-right font-semibold">MMS out</th>
                  <th className="px-2 py-1.5 text-right font-semibold">MMS in</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Calls out</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Calls in</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Min out</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Min in</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Usage rev.</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Paid</th>
                  <th className="px-2 py-1.5 text-right font-semibold">Blocked</th>
                </tr>
              </thead>
              <tbody>
                {series.map((day) => (
                  <tr key={day.date} className="border-t border-border">
                    <td className="px-2 py-1.5 text-left">{day.date}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.sms_out_segments)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.sms_in_segments)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.mms_out)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.mms_in)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.calls_out)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.calls_in)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.minutes_out)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.minutes_in)}</td>
                    <td className="px-2 py-1.5 text-right">{formatMicros(day.usage_revenue)}</td>
                    <td className="px-2 py-1.5 text-right">{formatMicros(day.paid)}</td>
                    <td className="px-2 py-1.5 text-right">{count(day.blocked_total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <SectionLabel>Payments</SectionLabel>
        {payments.length === 0 ? (
          <ConsoleEmpty>No payments in this window.</ConsoleEmpty>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {payments.map((p) => (
              <li key={p.id} className="rounded-[var(--cx-r-sm,12px)] border border-border p-2 text-[12.5px]">
                <span className="font-medium">{p.kind}</span> · {p.state} · {formatMicros(p.paid_micros)}
                {p.discount_micros > 0 ? ` (discount ${formatMicros(p.discount_micros)})` : ""}
                {p.paid_at ? ` · ${new Date(p.paid_at).toLocaleString()}` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <SectionLabel>Refusals</SectionLabel>
        {refusals.length === 0 ? (
          <ConsoleEmpty>No refusals in this window.</ConsoleEmpty>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {refusals.map((r, i) => (
              <li key={i} className="rounded-[var(--cx-r-sm,12px)] border border-border p-2 text-[12.5px]">
                <span className="font-medium">{r.kind}</span>
                {r.reason ? ` · ${r.reason}` : ""} · balance {formatMicros(r.balance_micros)}
                {r.at ? ` · ${new Date(r.at).toLocaleString()}` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <SectionLabel>Ledger (last 100)</SectionLabel>
        {ledger.length === 0 ? (
          <ConsoleEmpty>No ledger entries.</ConsoleEmpty>
        ) : (
          <ul className="mt-2 max-h-64 space-y-1.5 overflow-auto">
            {ledger.map((entry) => (
              <li key={entry.seq} className="rounded-[var(--cx-r-sm,12px)] border border-border p-2 text-[12.5px]">
                <span className="font-medium">{entry.type}</span> · {formatMicros(entry.amount_micros)} ·
                balance {formatMicros(entry.balance_after_micros)}
                {entry.note ? ` · ${entry.note}` : ""}
                {entry.at ? ` · ${new Date(entry.at).toLocaleString()}` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <SectionLabel>Numbers</SectionLabel>
        {numbers.length === 0 ? (
          <ConsoleEmpty>No numbers.</ConsoleEmpty>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {numbers.map((n) => (
              <li key={n.e164} className="rounded-[var(--cx-r-sm,12px)] border border-border p-2 text-[12.5px]">
                {n.e164} · {n.carrier ?? "unknown carrier"} · {n.status}
              </li>
            ))}
          </ul>
        )}
      </div>

      <SurfaceCard className="space-y-4">
        <SectionLabel>Admin actions</SectionLabel>
        <AdjustmentForm orgId={orgId} />
        <BundleForm orgId={orgId} />
        <PrepaidToggle orgId={orgId} enabled={org.prepaid} />
      </SurfaceCard>
    </div>
  );
}

function OrgDrawer({
  orgId,
  orgName,
  range,
  onClose,
}: {
  orgId: string | null;
  orgName: string;
  range: DateRange;
  onClose: () => void;
}): JSX.Element {
  return (
    <Drawer open={orgId != null} onClose={onClose} title={orgName} width="w-[560px]">
      {orgId != null ? <OrgDetailBody orgId={orgId} range={range} /> : null}
    </Drawer>
  );
}

// -----------------------------------------------------------------------------------------
// Payments section
// -----------------------------------------------------------------------------------------

function PaymentsSection({ range }: { range: DateRange }): JSX.Element {
  const { api } = useAuth();
  const payments = useConsolePayments(api, range);

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Payments</SectionLabel>
      {payments.isPending ? (
        <Spinner label="Loading payments" />
      ) : payments.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(payments.error)}
        </p>
      ) : payments.data && payments.data.length > 0 ? (
        <div className="overflow-x-auto rounded-[var(--cx-r-md,14px)] border border-border">
          <table className="w-full min-w-[720px] border-collapse text-[13px]">
            <thead>
              <tr className="bg-muted text-muted-foreground">
                <th className="px-3 py-2 text-left font-semibold">Workspace</th>
                <th className="px-3 py-2 text-left font-semibold">Kind</th>
                <th className="px-3 py-2 text-left font-semibold">State</th>
                <th className="px-3 py-2 text-right font-semibold">Paid</th>
                <th className="px-3 py-2 text-right font-semibold">Discount</th>
                <th className="px-3 py-2 text-left font-semibold">When</th>
              </tr>
            </thead>
            <tbody>
              {payments.data.map((p) => (
                <tr key={p.id} className="border-t border-border">
                  <td className="px-3 py-2 text-left">{p.org_name ?? p.org_id}</td>
                  <td className="px-3 py-2 text-left">{p.kind}</td>
                  <td className="px-3 py-2 text-left">{p.state}</td>
                  <td className="px-3 py-2 text-right">{formatMicros(p.paid_micros)}</td>
                  <td className="px-3 py-2 text-right">{formatMicros(p.discount_micros)}</td>
                  <td className="px-3 py-2 text-left">
                    {p.paid_at ? new Date(p.paid_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <ConsoleEmpty>No payments in this window.</ConsoleEmpty>
      )}
    </SurfaceCard>
  );
}

// -----------------------------------------------------------------------------------------
// Price list section
// -----------------------------------------------------------------------------------------

function PriceRow({ price }: { price: ConsolePrice }): JSX.Element {
  const { api } = useAuth();
  const update = useUpdateConsolePrice(api);
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState(() => priceMicrosToDollarsString(price.price_micros));
  const [note, setNote] = React.useState(price.note ?? "");

  const parsedMicros = priceDollarsStringToMicros(value);
  const valid = parsedMicros !== null;

  function startEdit() {
    setValue(priceMicrosToDollarsString(price.price_micros));
    setNote(price.note ?? "");
    setEditing(true);
  }

  function handleSave() {
    if (parsedMicros === null) return;
    update.mutate(
      { metric: price.metric, priceMicros: parsedMicros, note: note.trim() || undefined },
      { onSuccess: () => setEditing(false) },
    );
  }

  return (
    <tr className="border-t border-border">
      <td className="px-3 py-2 text-left font-medium">{price.metric}</td>
      <td className="px-3 py-2 text-left text-muted-foreground">{priceUnitLabel(price.metric)}</td>
      <td className="px-3 py-2 text-right">
        {editing ? (
          <Input
            aria-label={`Price for ${price.metric} in dollars`}
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-28"
          />
        ) : (
          `$${priceMicrosToDollarsString(price.price_micros)}`
        )}
      </td>
      <td className="px-3 py-2 text-left">
        {editing ? (
          <Input
            aria-label={`Note for ${price.metric}`}
            placeholder="Note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className="w-40"
          />
        ) : (
          price.note ?? "—"
        )}
      </td>
      <td className="px-3 py-2 text-right">
        {editing ? (
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" disabled={!valid || update.isPending} onClick={handleSave}>
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <Button type="button" size="sm" variant="outline" onClick={startEdit}>
            Edit
          </Button>
        )}
        {update.isError && editing ? (
          <p role="alert" className="mt-1 text-xs text-destructive">
            {mutationErrorMessage(update.error)}
          </p>
        ) : null}
      </td>
    </tr>
  );
}

function PriceListSection(): JSX.Element {
  const { api } = useAuth();
  const prices = useConsolePrices(api);

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Price list</SectionLabel>
      {prices.isPending ? (
        <Spinner label="Loading prices" />
      ) : prices.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(prices.error)}
        </p>
      ) : prices.data && prices.data.length > 0 ? (
        <div className="overflow-x-auto rounded-[var(--cx-r-md,14px)] border border-border">
          <table className="w-full min-w-[720px] border-collapse text-[13px]">
            <thead>
              <tr className="bg-muted text-muted-foreground">
                <th className="px-3 py-2 text-left font-semibold">Metric</th>
                <th className="px-3 py-2 text-left font-semibold">Unit</th>
                <th className="px-3 py-2 text-right font-semibold">Price</th>
                <th className="px-3 py-2 text-left font-semibold">Note</th>
                <th className="px-3 py-2 text-right font-semibold" />
              </tr>
            </thead>
            <tbody>
              {prices.data.map((price) => (
                <PriceRow key={price.metric} price={price} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <ConsoleEmpty>No prices configured.</ConsoleEmpty>
      )}
    </SurfaceCard>
  );
}

// -----------------------------------------------------------------------------------------
// Top level
// -----------------------------------------------------------------------------------------

export function ConsoleTab(): JSX.Element {
  const { api } = useAuth();
  const [range, setRange] = React.useState<DateRange>(() => {
    const { from, to } = lastNDaysRange(30);
    return { start: from, end: to };
  });
  const [openOrgId, setOpenOrgId] = React.useState<string | null>(null);

  const orgsQuery = useConsoleOrgs(api, range);

  const openOrgName =
    orgsQuery.data?.orgs.find((o) => o.org_id === openOrgId)?.name ?? "Workspace";

  return (
    <div className="space-y-5">
      <RangePicker range={range} onChange={setRange} />

      {orgsQuery.isPending ? (
        <Spinner label="Loading console" />
      ) : orgsQuery.isError || !orgsQuery.data ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(orgsQuery.error)}
        </p>
      ) : (
        <>
          <KpiGrid totals={orgsQuery.data.totals} summary={orgsQuery.data.summary} />
          <TrafficSection totals={orgsQuery.data.totals} />
          <div>
            <SectionLabel>Workspaces</SectionLabel>
            <div className="mt-2">
              <OrgsTable orgs={orgsQuery.data.orgs} onOpenOrg={setOpenOrgId} />
            </div>
          </div>
        </>
      )}

      <PaymentsSection range={range} />
      <PriceListSection />

      <OrgDrawer
        orgId={openOrgId}
        orgName={openOrgName}
        range={range}
        onClose={() => setOpenOrgId(null)}
      />
    </div>
  );
}
