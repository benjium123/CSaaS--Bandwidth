import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useAssignCampaign,
  useAvailableNumbers,
  useCampaigns,
  useNumbers,
  useOrderNumber,
  useReleaseNumber,
  type AvailableNumberFilters,
  type NumberOut,
} from "@/api/hooks";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const CARRIER_OPTIONS = [
  { value: "", label: "Primary carrier" },
  { value: "bandwidth", label: "Bandwidth" },
  { value: "telnyx", label: "Telnyx" },
  { value: "signalwire", label: "SignalWire" },
];

function registrationBadgeClass(registration: string): string {
  switch (registration) {
    case "approved":
      return "bg-green-100 text-green-800";
    case "pending":
      return "bg-amber-100 text-amber-800";
    case "rejected":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

export function NumbersPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const { data, isLoading } = useNumbers(api);
  const { data: campaigns } = useCampaigns(api);
  const campaignName = React.useCallback(
    (id: string | null | undefined) => campaigns?.find((c) => c.id === id)?.name ?? null,
    [campaigns],
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
    setError(null);
    try {
      await releaseNumber.mutateAsync(id);
      setConfirmReleaseId(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function assign(numberId: string, campaignId: string) {
    setError(null);
    try {
      await assignCampaign.mutateAsync({ numberId, campaign_id: campaignId || null });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 p-6">
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Numbers</h1>
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
        ) : (data ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No numbers yet.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Number</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Carrier</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Registration</th>
                  <th className="px-3 py-2 font-medium">Campaign</th>
                  <th className="px-3 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {(data ?? []).map((n) => (
                  <NumberRow
                    key={n.id}
                    number={n}
                    campaignName={campaignName(n.campaign_id)}
                    campaigns={campaigns ?? []}
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
      </div>

      <OrderNumberSection api={api} campaigns={campaigns ?? []} onOrdered={() => setError(null)} />
    </div>
  );
}

function NumberRow({
  number,
  campaignName,
  campaigns,
  onAssign,
  assignPending,
  confirming,
  onRelease,
  releasePending,
}: {
  number: NumberOut;
  campaignName: string | null;
  campaigns: { id: string; name: string }[];
  onAssign: (campaignId: string) => void;
  assignPending: boolean;
  confirming: boolean;
  onRelease: () => void;
  releasePending: boolean;
}) {
  const released = number.status === "released";

  return (
    <tr>
      <td className="px-3 py-2">{formatPhone(number.e164)}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{number.number_type}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{number.carrier}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{number.status}</td>
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
            className="h-8 rounded-md border border-border bg-background px-2 text-xs"
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
  const [error, setError] = React.useState<string | null>(null);

  const { data: results, isFetching } = useAvailableNumbers(
    api,
    searchFilters ?? {},
    searchFilters !== null,
  );

  const orderNumber = useOrderNumber(api);

  function search(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSearchFilters({
      area_code: areaCode || undefined,
      contains: contains || undefined,
      number_type: numberType,
      carrier: carrier || undefined,
    });
  }

  async function order(e164: string) {
    setError(null);
    try {
      await orderNumber.mutateAsync({ e164, carrier: carrier || undefined });
      onOrdered();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Order a number</h2>
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
          <select
            id="number-type"
            aria-label="Number type"
            className="h-9 rounded-md border border-border bg-background px-2 text-sm"
            value={numberType}
            onChange={(e) => setNumberType(e.target.value)}
          >
            <option value="local">Local</option>
            <option value="tollfree">Toll-free</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="carrier">
            Carrier
          </label>
          <select
            id="carrier"
            aria-label="Carrier"
            className="h-9 rounded-md border border-border bg-background px-2 text-sm"
            value={carrier}
            onChange={(e) => setCarrier(e.target.value)}
          >
            {CARRIER_OPTIONS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={isFetching}>
          Search
        </Button>
      </form>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {isFetching ? (
        <Spinner label="Searching" />
      ) : results && results.length > 0 ? (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Number</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Region</th>
                <th className="px-3 py-2 font-medium">Locality</th>
                <th className="px-3 py-2 font-medium">Monthly cost</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {results.map((r) => (
                <tr key={r.e164}>
                  <td className="px-3 py-2">{formatPhone(r.e164)}</td>
                  <td className={cn("px-3 py-2 text-xs text-muted-foreground")}>
                    {r.number_type}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.region}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.locality}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{r.monthly_cost}</td>
                  <td className="px-3 py-2">
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => order(r.e164)}
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
        <p className="text-sm text-muted-foreground">No numbers found.</p>
      ) : null}

      {campaigns.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No campaigns registered yet — ordered local numbers can be assigned to one later from
          the table above.
        </p>
      )}
    </div>
  );
}
