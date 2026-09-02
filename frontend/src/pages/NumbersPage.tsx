import * as React from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useAssignCampaign,
  useCampaigns,
  useCarrierCatalog,
  useReleaseNumber,
  type CarrierCatalogOut,
} from "@/api/hooks";
import {
  fetchProviderAccounts,
  PROVIDER_NAMES,
  type ProviderAccount,
  type ProviderName,
} from "@/api/providers";
import {
  formatMonthlyCost,
  formatSetupCost,
  useAvailableNumbers,
  useNumbers,
  useOrderNumber,
  type AvailableNumberFilters,
  type NumberOut,
  type SearchOut,
} from "@/api/numbers";
import { formatMicros, monthToDateRange, useSpendSummary } from "@/api/spend";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const PROVIDER_LABELS: Record<ProviderName, string> = {
  bandwidth: "Bandwidth",
  telnyx: "Telnyx",
  twilio: "Twilio",
  plivo: "Plivo",
  signalwire: "SignalWire",
};

type CarrierOption = {
  value: ProviderName;
  label: string;
  disabled: boolean;
  tooltip?: string;
};

/** Live carriers = env-live catalog entries UNION active provider accounts (P17). Every
 * provider always renders, in the fixed order the plan calls for - not-live ones are
 * disabled with a tooltip instead of being hidden. */
function buildCarrierOptions(
  catalog: CarrierCatalogOut[],
  accounts: ProviderAccount[],
): CarrierOption[] {
  const activeAccountByProvider = new Map<ProviderName, ProviderAccount>();
  for (const account of accounts) {
    if (account.status === "active" && !activeAccountByProvider.has(account.provider)) {
      activeAccountByProvider.set(account.provider, account);
    }
  }
  const catalogByProvider = new Map(catalog.map((entry) => [entry.name, entry]));

  return PROVIDER_NAMES.map((name) => {
    const account = activeAccountByProvider.get(name);
    const entry = catalogByProvider.get(name);
    const envLive = Boolean(entry?.live);
    const live = Boolean(account) || envLive;

    const label = account
      ? `${PROVIDER_LABELS[name]} (account: ${account.label})`
      : envLive
        ? `${PROVIDER_LABELS[name]} (env)`
        : PROVIDER_LABELS[name];

    return {
      value: name,
      label,
      disabled: !live,
      tooltip: live ? undefined : entry?.reason || "Add credentials in Providers",
    };
  });
}

function registrationBadgeClass(registration: string): string {
  switch (registration) {
    case "approved":
      return "bg-green-950 text-green-400";
    case "pending":
      return "bg-amber-950 text-amber-400";
    case "rejected":
      return "bg-red-950 text-red-400";
    default:
      return "bg-neutral-800 text-neutral-400";
  }
}

function numberStatusPill(status: string): { label: string; className: string } {
  switch (status) {
    case "active":
      return { label: "Active", className: "bg-green-950 text-green-400" };
    case "pending":
      return { label: "Pending", className: "bg-amber-950 text-amber-400" };
    case "failed":
      return { label: "Failed", className: "bg-red-950 text-red-400" };
    case "released":
      return { label: "Released", className: "bg-neutral-800 text-neutral-400" };
    default:
      return { label: status, className: "bg-neutral-800 text-neutral-400" };
  }
}

function formatPurchasedAt(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString();
}

/** Small inline pending/error readout - same local pattern as ProvidersPage /
 * InboxSettingsPage (P16/P17); not shared since neither exports it. */
function MutationStatus({
  mutation,
}: {
  mutation: { isPending: boolean; isError: boolean; error: unknown };
}) {
  if (mutation.isPending) {
    return (
      <span className="flex items-center gap-1 text-[10px] text-neutral-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Saving…
      </span>
    );
  }
  if (mutation.isError) {
    return (
      <span role="alert" className="text-[10px] text-red-400">
        {(mutation.error as Error).message}
      </span>
    );
  }
  return null;
}

export function NumbersPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const { data: numbers, isLoading } = useNumbers(api);
  const { data: campaigns } = useCampaigns(api);
  const campaignName = React.useCallback(
    (id: string | null | undefined) => campaigns?.find((c) => c.id === id)?.name ?? null,
    [campaigns],
  );

  const spendRange = React.useMemo(() => monthToDateRange(), []);
  const spendSummaryQuery = useSpendSummary(api, spendRange.from, spendRange.to);
  const spendMicrosByNumberId = React.useCallback(
    (numberId: string, carrier: string): number | undefined => {
      const providerSpend = spendSummaryQuery.data?.by_provider[carrier];
      return providerSpend?.numbers.find((n) => n.number_id === numberId)?.cost_micros;
    },
    [spendSummaryQuery.data],
  );

  const [value, setValue] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.request("/api/v1/numbers", { method: "POST", json: { e164: value } });
      setValue("");
      qc.invalidateQueries({ queryKey: ["numbers"] });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const releaseNumber = useReleaseNumber(api);
  const assignCampaign = useAssignCampaign(api);
  const [confirmReleaseId, setConfirmReleaseId] = React.useState<string | null>(null);

  async function release(id: string) {
    try {
      await releaseNumber.mutateAsync(id);
      setConfirmReleaseId(null);
    } catch {
      // surfaced via MutationStatus below
    }
  }

  async function assign(numberId: string, campaignId: string) {
    try {
      await assignCampaign.mutateAsync({ numberId, campaign_id: campaignId || null });
    } catch {
      // surfaced via MutationStatus below
    }
  }

  return (
    <div className="dark mx-auto max-w-5xl space-y-8 bg-neutral-950 p-6 text-neutral-100">
      <div className="space-y-4">
        <div className="space-y-2">
          <h1 className="text-lg font-semibold text-neutral-50">Numbers</h1>
          <p className="text-sm text-neutral-400">
            Search, order, release, and assign org numbers.
          </p>
        </div>

        <form className="flex gap-2" onSubmit={add}>
          <Input
            aria-label="Phone number"
            placeholder="+12145550100"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button type="submit">Add</Button>
        </form>
        {error && (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        )}

        {isLoading ? (
          <Spinner />
        ) : (numbers ?? []).length === 0 ? (
          <p className="text-sm text-neutral-400">No numbers yet.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-neutral-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-neutral-800 text-left text-xs text-neutral-400">
                  <th className="px-3 py-2 font-medium">Number</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Carrier</th>
                  <th className="px-3 py-2 font-medium">Cost</th>
                  <th className="px-3 py-2 font-medium">
                    Spend MTD <span className="font-normal text-neutral-600">(UTC days)</span>
                  </th>
                  <th className="px-3 py-2 font-medium">Purchased</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Registration</th>
                  <th className="px-3 py-2 font-medium">Campaign</th>
                  <th className="px-3 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-800">
                {(numbers ?? []).map((n) => (
                  <NumberRow
                    key={n.id}
                    number={n}
                    campaignName={campaignName(n.campaign_id)}
                    campaigns={campaigns ?? []}
                    spendMicros={spendMicrosByNumberId(n.id, n.carrier)}
                    spendUnavailable={spendSummaryQuery.isLoading || spendSummaryQuery.isError}
                    onAssign={(campaignId) => assign(n.id, campaignId)}
                    assignPending={assignCampaign.isPending}
                    confirming={confirmReleaseId === n.id}
                    onRelease={() => {
                      if (confirmReleaseId === n.id) {
                        void release(n.id);
                      } else {
                        setConfirmReleaseId(n.id);
                      }
                    }}
                    releasePending={releaseNumber.isPending}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center gap-3">
          <MutationStatus mutation={releaseNumber} />
          <MutationStatus mutation={assignCampaign} />
        </div>
      </div>

      <OrderNumberSection api={api} campaigns={campaigns ?? []} onOrdered={() => setError(null)} />
    </div>
  );
}

function NumberRow({
  number,
  campaignName,
  campaigns,
  spendMicros,
  spendUnavailable,
  onAssign,
  assignPending,
  confirming,
  onRelease,
  releasePending,
}: {
  number: NumberOut;
  campaignName: string | null;
  campaigns: { id: string; name: string }[];
  spendMicros: number | undefined;
  spendUnavailable: boolean;
  onAssign: (campaignId: string) => void;
  assignPending: boolean;
  confirming: boolean;
  onRelease: () => void;
  releasePending: boolean;
}) {
  const released = number.status === "released";
  const status = numberStatusPill(number.status);

  return (
    <tr>
      <td className="px-3 py-2 text-neutral-100">{formatPhone(number.e164)}</td>
      <td className="px-3 py-2 text-xs text-neutral-400">{number.number_type}</td>
      <td className="px-3 py-2">
        <div className="text-xs text-neutral-200">{number.carrier}</div>
        {number.provider_account_label && (
          <div className="text-xs text-neutral-500">{number.provider_account_label}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-neutral-300">{formatMonthlyCost(number)}</td>
      <td className="px-3 py-2 text-xs text-neutral-300">
        {spendUnavailable ? "—" : formatMicros(spendMicros ?? 0)}
      </td>
      <td className="px-3 py-2 text-xs text-neutral-400">{formatPurchasedAt(number.purchased_at)}</td>
      <td className="px-3 py-2">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs",
            status.className,
          )}
        >
          {number.status === "pending" && <Loader2 className="h-3 w-3 animate-spin" />}
          {status.label}
        </span>
        {number.status === "failed" && number.order_detail && (
          <span className="mt-1 block max-w-[240px] text-xs text-red-400">
            {number.order_detail}
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <Badge
          className={registrationBadgeClass(number.registration)}
          title={number.registration_detail || undefined}
        >
          {number.registration}
        </Badge>
      </td>
      <td className="px-3 py-2">
        {number.number_type === "local" ? (
          <select
            aria-label={`Campaign for ${number.e164}`}
            className="h-8 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100"
            value={number.campaign_id ?? ""}
            onChange={(e) => onAssign(e.target.value)}
            disabled={assignPending || released}
          >
            <option value="">—</option>
            {campaigns.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-xs text-neutral-400">{campaignName ?? "—"}</span>
        )}
      </td>
      <td className="px-3 py-2">
        {released ? (
          <span className="text-xs text-neutral-500">Released</span>
        ) : (
          <Button
            type="button"
            size="sm"
            variant={confirming ? "destructive" : "outline"}
            onClick={onRelease}
            disabled={releasePending}
          >
            {confirming ? "Confirm release" : "Release"}
          </Button>
        )}
      </td>
    </tr>
  );
}

function OrderNumberSection({
  api,
  campaigns,
  onOrdered,
}: {
  api: ApiClient;
  campaigns: { id: string; name: string }[];
  onOrdered: () => void;
}) {
  const [areaCode, setAreaCode] = React.useState("");
  const [contains, setContains] = React.useState("");
  const [numberType, setNumberType] = React.useState("local");
  const [carrier, setCarrier] = React.useState("");
  const [searchFilters, setSearchFilters] = React.useState<AvailableNumberFilters | null>(null);
  const [orderedNumber, setOrderedNumber] = React.useState<{ e164: string; status: string } | null>(
    null,
  );

  const { data: catalog } = useCarrierCatalog(api);
  const providerAccountsQuery = useQuery({
    queryKey: ["provider-accounts"],
    queryFn: () => fetchProviderAccounts(api),
  });
  const carrierOptions = React.useMemo(
    () => buildCarrierOptions(catalog ?? [], providerAccountsQuery.data ?? []),
    [catalog, providerAccountsQuery.data],
  );

  const availableQuery = useAvailableNumbers(api, searchFilters ?? {}, searchFilters !== null);
  const results: SearchOut[] = availableQuery.data ?? [];

  const orderNumber = useOrderNumber(api);

  function search(e: React.FormEvent) {
    e.preventDefault();
    setOrderedNumber(null);
    setSearchFilters({
      area_code: areaCode || undefined,
      contains: contains || undefined,
      number_type: numberType,
      carrier: carrier || undefined,
    });
  }

  async function order(result: SearchOut) {
    setOrderedNumber(null);
    try {
      const ordered = await orderNumber.mutateAsync({
        e164: result.e164,
        // The carrier the RESULTS were fetched with, not whatever the live dropdown
        // state is now - the operator may have changed the dropdown after searching,
        // and the result row's carrier must match what was actually searched/shown.
        carrier: searchFilters?.carrier,
        // Only send the cost fields the row actually priced - omit them entirely
        // (rather than sending null) when the provider didn't quote a cents amount.
        ...(typeof result.monthly_cost_cents === "number"
          ? { monthly_cost_cents: result.monthly_cost_cents }
          : {}),
        ...(typeof result.setup_cost_cents === "number"
          ? { setup_cost_cents: result.setup_cost_cents }
          : {}),
      });
      setOrderedNumber({ e164: ordered.e164, status: ordered.status });
      onOrdered();
    } catch {
      // surfaced via MutationStatus below
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold text-neutral-50">Order a number</h2>
      <form className="flex flex-wrap items-end gap-2" onSubmit={search}>
        <div className="space-y-1">
          <label className="block text-xs text-neutral-400" htmlFor="area-code">
            Area code
          </label>
          <Input
            id="area-code"
            aria-label="Area code"
            placeholder="214"
            className="w-24"
            value={areaCode}
            onChange={(e) => setAreaCode(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-neutral-400" htmlFor="contains">
            Contains
          </label>
          <Input
            id="contains"
            aria-label="Contains"
            className="w-28"
            value={contains}
            onChange={(e) => setContains(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-neutral-400" htmlFor="number-type">
            Type
          </label>
          <select
            id="number-type"
            aria-label="Number type"
            className="h-9 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100"
            value={numberType}
            onChange={(e) => setNumberType(e.target.value)}
          >
            <option value="local">Local</option>
            <option value="tollfree">Toll-free</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-neutral-400" htmlFor="carrier">
            Carrier
          </label>
          <select
            id="carrier"
            aria-label="Carrier"
            className="h-9 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100"
            value={carrier}
            onChange={(e) => setCarrier(e.target.value)}
          >
            <option value="">Any live provider</option>
            {carrierOptions.map((option) => (
              <option
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                title={option.tooltip}
              >
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={availableQuery.isFetching}>
          Search
        </Button>
        <MutationStatus mutation={orderNumber} />
      </form>

      {orderedNumber && (
        <div className="rounded-md border border-green-800 bg-green-950 p-3 text-sm text-green-200">
          Ordered {orderedNumber.e164} ({orderedNumber.status}) —{" "}
          <Link to="/settings/inboxes" className="text-green-400 underline">
            Grant this inbox to a department or employee →
          </Link>
        </div>
      )}

      {availableQuery.isFetching ? (
        <Spinner label="Searching" />
      ) : availableQuery.isError ? (
        <p role="alert" className="text-sm text-red-400">
          {(availableQuery.error as Error).message}
        </p>
      ) : results.length > 0 ? (
        <div className="overflow-x-auto rounded-md border border-neutral-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-neutral-800 text-left text-xs text-neutral-400">
                <th className="px-3 py-2 font-medium">Number</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Region</th>
                <th className="px-3 py-2 font-medium">Locality</th>
                <th className="px-3 py-2 font-medium">Monthly cost</th>
                <th className="px-3 py-2 font-medium">Setup cost</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800">
              {results.map((r) => (
                <tr key={r.e164}>
                  <td className="px-3 py-2 text-neutral-100">{formatPhone(r.e164)}</td>
                  <td className={cn("px-3 py-2 text-xs text-neutral-400")}>{r.number_type}</td>
                  <td className="px-3 py-2 text-xs text-neutral-400">{r.region}</td>
                  <td className="px-3 py-2 text-xs text-neutral-400">{r.locality}</td>
                  <td className="px-3 py-2 text-xs text-neutral-300">{formatMonthlyCost(r)}</td>
                  <td className="px-3 py-2 text-xs text-neutral-300">
                    {formatSetupCost(r.setup_cost_cents)}
                  </td>
                  <td className="px-3 py-2">
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => order(r)}
                      disabled={orderNumber.isPending}
                    >
                      Order
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : searchFilters ? (
        <p className="text-sm text-neutral-400">No numbers found.</p>
      ) : null}

      {campaigns.length === 0 && (
        <p className="text-xs text-neutral-500">
          No campaigns registered yet — ordered local numbers can be assigned to one later from
          the table above.
        </p>
      )}
    </div>
  );
}
