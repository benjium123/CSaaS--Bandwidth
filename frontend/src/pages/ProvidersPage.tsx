import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useCarrierCatalog,
  useProbeCarrier,
  useRoutingPolicy,
  useUpdateRoutingPolicy,
  type CarrierCatalogOut,
  type ProbeOut,
  type RoutingPolicyIn,
} from "@/api/hooks";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

type ProbeState = { kind: "result"; data: ProbeOut } | { kind: "error"; message: string };

function statusPill(entry: CarrierCatalogOut): { label: string; className: string } {
  if (entry.live) return { label: "Live", className: "bg-green-100 text-green-800" };
  if (entry.enabled_flag === false) return { label: "Off", className: "bg-gray-100 text-gray-600" };
  return { label: "Needs credentials", className: "bg-amber-100 text-amber-800" };
}

function hasMms(entry: CarrierCatalogOut): boolean {
  const bytes = entry.capabilities?.max_media_bytes;
  return typeof bytes === "number" && bytes > 0;
}

export function ProvidersPage() {
  const { api } = useAuth();
  const { data: catalog, isLoading } = useCarrierCatalog(api);
  const probeCarrier = useProbeCarrier(api);
  const [probing, setProbing] = React.useState<string | null>(null);
  const [results, setResults] = React.useState<Record<string, ProbeState>>({});

  const sorted = React.useMemo(() => {
    return [...(catalog ?? [])].sort((a, b) => {
      if (a.live !== b.live) return a.live ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  }, [catalog]);

  async function probe(name: string) {
    setProbing(name);
    try {
      const data = await probeCarrier.mutateAsync(name);
      setResults((r) => ({ ...r, [name]: { kind: "result", data } }));
    } catch (err) {
      setResults((r) => ({ ...r, [name]: { kind: "error", message: (err as Error).message } }));
    } finally {
      setProbing(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 p-6">
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Providers</h1>
        <p className="text-sm text-muted-foreground">
          Every carrier this build supports, whether it is live, and what to add to make it
          live.
        </p>

        {isLoading ? (
          <Spinner />
        ) : sorted.length === 0 ? (
          <p className="text-sm text-muted-foreground">No carriers found.</p>
        ) : (
          <ul aria-label="Carriers" className="space-y-3">
            {sorted.map((entry) => (
              <CarrierCard
                key={entry.name}
                entry={entry}
                onProbe={() => probe(entry.name)}
                probing={probing === entry.name}
                result={results[entry.name]}
              />
            ))}
          </ul>
        )}
      </div>

      <PolicySection api={api} />
    </div>
  );
}

function CarrierCard({
  entry,
  onProbe,
  probing,
  result,
}: {
  entry: CarrierCatalogOut;
  onProbe: () => void;
  probing: boolean;
  result: ProbeState | undefined;
}) {
  const pill = statusPill(entry);
  const breakerLoud = entry.live && Boolean(entry.state) && entry.state !== "closed";

  return (
    <li className="space-y-3 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium capitalize">{entry.name}</span>
          <Badge className={pill.className}>{pill.label}</Badge>
          {entry.live && entry.state && (
            <Badge
              role={breakerLoud ? "alert" : undefined}
              className={cn(
                breakerLoud ? "bg-red-100 font-semibold text-red-800" : "bg-gray-100 text-gray-600",
              )}
            >
              {breakerLoud ? `Breaker ${entry.state} — failing` : `Breaker ${entry.state}`}
            </Badge>
          )}
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onProbe}
          disabled={entry.missing.length > 0 || probing}
        >
          {probing ? "Testing…" : "Test credentials"}
        </Button>
      </div>

      <div className="flex flex-wrap gap-1">
        <Badge className="bg-gray-100 text-gray-600">SMS</Badge>
        {hasMms(entry) && <Badge className="bg-gray-100 text-gray-600">MMS</Badge>}
        {entry.supports_voice && <Badge className="bg-gray-100 text-gray-600">Voice</Badge>}
        {entry.supports_numbers && <Badge className="bg-gray-100 text-gray-600">Numbers</Badge>}
        {entry.primary && <Badge className="bg-blue-100 text-blue-800">Primary</Badge>}
      </div>

      {!entry.live && (
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>{entry.reason}</p>
          {entry.missing.length > 0 && (
            <div className="space-y-1">
              <code className="block rounded bg-muted p-2 text-[11px]">
                {entry.missing.map((name) => (
                  <div key={name}>{name}</div>
                ))}
              </code>
              <p>Add these to the server .env and restart.</p>
            </div>
          )}
        </div>
      )}

      {result && result.kind === "error" && (
        <p role="alert" className="text-xs text-destructive">
          {result.message}
        </p>
      )}
      {result && result.kind === "result" && result.data.ok && (
        <p className="text-xs text-green-700">{result.data.detail}</p>
      )}
      {result && result.kind === "result" && !result.data.ok && (
        <div className="space-y-1">
          <p role="alert" className="text-xs text-destructive">
            {result.data.detail}
          </p>
          <p className="break-all font-mono text-[10px] text-muted-foreground">
            {result.data.checked}
          </p>
        </div>
      )}
    </li>
  );
}

function PolicySection({ api }: { api: ApiClient }) {
  const { data: policy, isLoading } = useRoutingPolicy(api);
  const updatePolicy = useUpdateRoutingPolicy(api);
  const [error, setError] = React.useState<string | null>(null);

  async function patch(vars: RoutingPolicyIn) {
    setError(null);
    try {
      await updatePolicy.mutateAsync(vars);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function move(name: string, direction: -1 | 1) {
    if (!policy) return;
    const idx = policy.preference.indexOf(name);
    const next = idx + direction;
    if (idx < 0 || next < 0 || next >= policy.preference.length) return;
    const reordered = [...policy.preference];
    const [item] = reordered.splice(idx, 1);
    reordered.splice(next, 0, item);
    void patch({ preference: reordered });
  }

  return (
    <div className="space-y-4 border-t border-border pt-6">
      <h2 className="text-base font-semibold">Routing policy</h2>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {isLoading || !policy ? (
        <Spinner label="Loading routing policy" />
      ) : (
        <>
          {policy.preference.length === 0 ? (
            <p className="text-sm text-muted-foreground">No carriers in the preference order yet.</p>
          ) : (
            <ol aria-label="Carrier preference order" className="max-w-md space-y-1">
              {policy.preference.map((name, i) => (
                <li
                  key={name}
                  className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-1.5 text-sm"
                >
                  <span>
                    {i + 1}. {name}
                  </span>
                  <div className="flex gap-1">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${name} up`}
                      onClick={() => move(name, -1)}
                      disabled={i === 0 || updatePolicy.isPending}
                    >
                      ↑
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${name} down`}
                      onClick={() => move(name, 1)}
                      disabled={i === policy.preference.length - 1 || updatePolicy.isPending}
                    >
                      ↓
                    </Button>
                  </div>
                </li>
              ))}
            </ol>
          )}

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={policy.allow_intra_carrier_failover}
              onChange={(e) => patch({ allow_intra_carrier_failover: e.target.checked })}
              disabled={updatePolicy.isPending}
            />
            Allow intra-carrier failover
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={policy.allow_cross_carrier_failover}
              onChange={(e) => patch({ allow_cross_carrier_failover: e.target.checked })}
              disabled={updatePolicy.isPending}
            />
            Allow cross-carrier failover
          </label>
        </>
      )}
    </div>
  );
}
