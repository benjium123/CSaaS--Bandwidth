import * as React from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useAgentProfiles,
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
  answeredBy,
  formatMonthlyCost,
  formatSetupCost,
  useAvailableNumbers,
  useNumbers,
  useOrderNumber,
  useSetAnsweredBy,
  type AvailableNumberFilters,
  type NumberOut,
  type SearchOut,
} from "@/api/numbers";
import { formatMicros, monthToDateRange, useSpendSummary } from "@/api/spend";
import {
  Button,
  Card,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

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
        ? `${PROVIDER_LABELS[name]} (shared)`
        : PROVIDER_LABELS[name];

    return {
      value: name,
      label,
      disabled: !live,
      tooltip: live ? undefined : entry?.reason || "Add credentials in Providers",
    };
  });
}

function registrationTone(registration: string): PillTone {
  switch (registration) {
    case "approved":
      return "success";
    case "pending":
      return "warning";
    case "rejected":
      return "danger";
    default:
      return "neutral";
  }
}

function numberStatusPill(status: string): { label: string; tone: PillTone } {
  switch (status) {
    case "active":
      return { label: "Active", tone: "success" };
    case "pending":
      return { label: "Pending", tone: "warning" };
    case "failed":
      return { label: "Failed", tone: "danger" };
    case "released":
      return { label: "Released", tone: "neutral" };
    default:
      return { label: status, tone: "neutral" };
  }
}

function providerDisplayLabel(number: NumberOut): string {
  const friendly = (PROVIDER_LABELS as Record<string, string | undefined>)[number.carrier];
  if (friendly) return friendly;
  if (number.provider_account_label) return number.provider_account_label;
  return "—";
}

function formatPurchasedAt(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString();
}

export function NumbersPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const { data: numbers, isLoading, isError, error: numbersError, refetch: refetchNumbers } =
    useNumbers(api);
  const { data: campaigns } = useCampaigns(api);
  // Same ["agent-profiles"] key the Assistants builder uses, so the two stay in step.
  const { data: assistants } = useAgentProfiles(api);
  const setAnsweredBy = useSetAnsweredBy(api);
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
    <div className="dark mx-auto max-w-5xl space-y-8 bg-background p-6 text-foreground">
      <Section
        title="Phone numbers"
        description="Search, order, release, and assign org numbers."
      >
        <div className="space-y-4">
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
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          {isLoading ? (
            <Spinner />
          ) : isError ? (
            <div role="alert" className="flex items-center gap-3 text-sm text-destructive">
              <span>{(numbersError as Error).message}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => refetchNumbers()}>
                Retry
              </Button>
            </div>
          ) : (numbers ?? []).length === 0 ? (
            <EmptyState
              title="No numbers yet"
              description="Add a number above or order one from the order section."
              action={
                <Button
                  type="button"
                  onClick={() => document.getElementById("order-a-number")?.scrollIntoView()}
                >
                  Order a number
                </Button>
              }
            />
          ) : (
            <Card className="overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2 font-medium">Number</th>
                    <th className="px-3 py-2 font-medium">Inbox</th>
                    {/* P23b: who picks up. Writing this binds a one-node flow to the number
                        (PATCH /numbers/{id}/answered-by); "Human" restores the seeded ring flow. */}
                    <th className="px-3 py-2 font-medium">Answered by</th>
                    <th className="px-3 py-2 font-medium">Type</th>
                    <th className="px-3 py-2 font-medium">Provider</th>
                    <th className="px-3 py-2 font-medium">Cost</th>
                    <th className="px-3 py-2 font-medium">
                      Spend MTD <span className="font-normal text-muted-foreground">(UTC days)</span>
                    </th>
                    <th className="px-3 py-2 font-medium">Purchased</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th
                      className="px-3 py-2 font-medium"
                      title="Registration status for SMS messaging"
                    >
                      SMS registration
                    </th>
                    <th className="px-3 py-2 font-medium">Campaign</th>
                    <th className="px-3 py-2 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(numbers ?? []).map((n) => (
                    <NumberRow
                      key={n.id}
                      number={n}
                      campaignName={campaignName(n.campaign_id)}
                      campaigns={campaigns ?? []}
                      assistants={assistants ?? []}
                      onAnsweredByChange={(mode, profileId) =>
                        setAnsweredBy.mutate({ numberId: n.id, mode, profile_id: profileId })
                      }
                      answeredByPending={
                        setAnsweredBy.isPending && setAnsweredBy.variables?.numberId === n.id
                      }
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
            </Card>
          )}
          <div className="flex items-center gap-3">
            {/* Small inline pending/error readout - same local pattern as ProvidersPage /
                InboxSettingsPage (P16/P17); not shared since neither exports it. */}
            <MutationStatus pending={releaseNumber.isPending} error={releaseNumber.error} pendingLabel="Saving…" />
            <MutationStatus pending={assignCampaign.isPending} error={assignCampaign.error} pendingLabel="Saving…" />
            <MutationStatus pending={setAnsweredBy.isPending} error={setAnsweredBy.error} pendingLabel="Saving…" />
          </div>
        </div>
      </Section>

      <OrderNumberSection api={api} campaigns={campaigns ?? []} onOrdered={() => setError(null)} />
    </div>
  );
}

function NumberRow({
  number,
  campaignName,
  campaigns,
  assistants,
  onAnsweredByChange,
  answeredByPending,
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
  assistants: { id: string; name: string }[];
  onAnsweredByChange: (mode: "human" | "assistant", profileId: string | null) => void;
  answeredByPending: boolean;
  spendMicros: number | undefined;
  spendUnavailable: boolean;
  onAssign: (campaignId: string) => void;
  assignPending: boolean;
  confirming: boolean;
  onRelease: () => void;
  releasePending: boolean;
}) {
  const released = number.status === "released";
  const current = answeredBy(number);
  const assistantName =
    current.mode === "assistant"
      ? (assistants.find((a) => a.id === current.profile_id)?.name ?? null)
      : null;
  const status = numberStatusPill(number.status);
  const providerLabel = providerDisplayLabel(number);

  return (
    <tr>
      <td className="px-3 py-2 text-foreground">{formatPhone(number.e164)}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {number.inbox_name ?? "Not in an inbox"}
      </td>
      <td className="px-3 py-2">
        {/* One control, one value: "A person" plus one entry per assistant. The value is the
            assistant's id, and the empty string means a person - so the two modes the wire
            has cannot get out of step with each other on screen. */}
        <Select
          aria-label={`Answered by for ${number.e164}`}
          className="h-8 px-2 text-xs text-foreground"
          value={current.mode === "assistant" ? (current.profile_id ?? "") : ""}
          onChange={(e) =>
            e.target.value
              ? onAnsweredByChange("assistant", e.target.value)
              : onAnsweredByChange("human", null)
          }
          disabled={answeredByPending || released}
        >
          <option value="">A person</option>
          {assistants.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
          {/* An assistant that has been deleted is still what answers this number until
              somebody changes it. Without this option the select would fall back to the
              first one and silently claim a person answers - a lie about live routing. */}
          {current.mode === "assistant" && current.profile_id && !assistantName && (
            <option value={current.profile_id}>An assistant that is no longer here</option>
          )}
        </Select>
        {current.mode === "assistant" && !assistantName && (
          <span className="mt-1 block text-[11px] text-muted-foreground">
            An assistant that is no longer here answers this number.
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{number.number_type}</td>
      <td className="px-3 py-2">
        <div className="text-xs text-foreground">{providerLabel}</div>
        {number.provider_account_label && providerLabel !== number.provider_account_label && (
          <div className="text-xs text-muted-foreground">{number.provider_account_label}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-foreground">{formatMonthlyCost(number)}</td>
      <td className="px-3 py-2 text-xs text-foreground">
        {spendUnavailable ? "—" : formatMicros(spendMicros ?? 0)}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{formatPurchasedAt(number.purchased_at)}</td>
      <td className="px-3 py-2">
        <Pill tone={status.tone} className="gap-1">
          {number.status === "pending" && <Loader2 className="h-3 w-3 animate-spin" />}
          {status.label}
        </Pill>
        {number.status === "failed" && number.order_detail && (
          <span className="mt-1 block max-w-[240px] text-xs text-destructive">
            {number.order_detail}
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        {/* registration_detail comes from the backend and may contain carrier campaign
            terms; keep the surrounding label plain and render the backend text as the tooltip. */}
        <Pill
          tone={registrationTone(number.registration)}
          title={number.registration_detail || undefined}
        >
          {number.registration}
        </Pill>
      </td>
      <td className="px-3 py-2">
        {number.number_type === "local" ? (
          <Select
            aria-label={`Campaign for ${number.e164}`}
            className="h-8 px-2 text-xs text-foreground"
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
          </Select>
        ) : (
          <span className="text-xs text-muted-foreground">{campaignName ?? "—"}</span>
        )}
      </td>
      <td className="px-3 py-2">
        {released ? (
          <span className="text-xs text-muted-foreground">Released</span>
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
    <Section id="order-a-number" title="Order a number">
      <form className="flex flex-wrap items-end gap-2" onSubmit={search}>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="area-code">
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
          <label className="block text-xs text-muted-foreground" htmlFor="contains">
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
          <label className="block text-xs text-muted-foreground" htmlFor="number-type">
            Type
          </label>
          <Select
            id="number-type"
            aria-label="Number type"
            value={numberType}
            onChange={(e) => setNumberType(e.target.value)}
          >
            <option value="local">Local</option>
            <option value="tollfree">Toll-free</option>
          </Select>
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="carrier">
            Provider
          </label>
          <Select
            id="carrier"
            aria-label="Provider"
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
          </Select>
        </div>
        <Button type="submit" disabled={availableQuery.isFetching}>
          Search
        </Button>
        <MutationStatus pending={orderNumber.isPending} error={orderNumber.error} pendingLabel="Saving…" />
      </form>

      {orderedNumber && (
        <Card className="flex items-center justify-between gap-3">
          <span>
            Ordered {orderedNumber.e164} ({orderedNumber.status}) —{" "}
            <Link to="/settings/inboxes" className="underline">
              Grant this inbox to a department or employee →
            </Link>
          </span>
        </Card>
      )}

      {availableQuery.isFetching ? (
        <Spinner label="Searching" />
      ) : availableQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {(availableQuery.error as Error).message}
        </p>
      ) : results.length > 0 ? (
        <Card className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Number</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Region</th>
                <th className="px-3 py-2 font-medium">Locality</th>
                <th className="px-3 py-2 font-medium">Monthly cost</th>
                <th className="px-3 py-2 font-medium">Setup cost</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {results.map((r) => (
                <tr key={r.e164}>
                  <td className="px-3 py-2 text-foreground">{formatPhone(r.e164)}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.number_type}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.region}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.locality}</td>
                  <td className="px-3 py-2 text-xs text-foreground">{formatMonthlyCost(r)}</td>
                  <td className="px-3 py-2 text-xs text-foreground">
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
        </Card>
      ) : searchFilters ? (
        <EmptyState
          title="No numbers found"
          description="Try a different area code, phrase, type, or provider."
          action={
            <Button type="button" variant="outline" onClick={() => setSearchFilters(null)}>
              Clear search
            </Button>
          }
        />
      ) : null}

      {campaigns.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No campaigns registered yet — ordered local numbers can be assigned to one later from
          the table above.
        </p>
      )}
    </Section>
  );
}
