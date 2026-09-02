import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { ApiError, type ApiClient } from "@/api/client";
import {
  useCarrierCatalog,
  useNumbers,
  useProbeCarrier,
  useRoutingPolicy,
  useUpdateRoutingPolicy,
  type CarrierCatalogOut,
  type NumberOut,
  type ProbeOut,
  type RoutingPolicyIn,
} from "@/api/hooks";
import {
  PROVIDER_FIELDS,
  PROVIDER_NAMES,
  SECRET_MASK,
  createProviderAccount,
  disableProviderAccount,
  fetchProviderAccounts,
  patchProviderAccount,
  probeProviderAccount,
  type PatchProviderAccountInput,
  type ProviderAccount,
  type ProviderAccountStatus,
  type ProviderName,
} from "@/api/providers";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { RatesDrawer } from "@/components/spend/RatesDrawer";
import { SpendCard } from "@/components/spend/SpendCard";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

type ProbeState = { kind: "result"; data: ProbeOut } | { kind: "error"; message: string };

function accountStatusPill(status?: ProviderAccountStatus): { label: string; className: string } {
  switch (status) {
    case "unverified":
      return { label: "Unverified", className: "bg-amber-950 text-amber-400" };
    case "active":
      return { label: "Active", className: "bg-green-950 text-green-400" };
    case "failed":
      return { label: "Failed", className: "bg-red-950 text-red-400" };
    case "disabled":
      return { label: "Disabled", className: "bg-neutral-800 text-neutral-400" };
    default:
      return { label: "Not configured", className: "bg-neutral-800 text-neutral-500" };
  }
}

/**
 * F5: for an EXISTING account the backend does partial validation and rejects "" for any
 * field (not just secrets) - so every blank value must be dropped, not just blank secrets.
 * For a NEW account every field is required up front (see createIncomplete below), so this
 * is only ever asked to build a full, non-blank payload there.
 */
function buildCredentialsForSave(
  provider: ProviderName,
  account: ProviderAccount | undefined,
  values: Record<string, string>,
): Record<string, string> {
  const next: Record<string, string> = {};
  for (const field of PROVIDER_FIELDS[provider]) {
    const value = values[field.name] ?? "";
    if (account && value === "") continue;
    next[field.name] = value;
  }
  return next;
}

function MutationStatus({
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
        {(mutation.error as Error).message}
      </span>
    );
  }
  if (mutation.isSuccess) {
    return <span className="text-[10px] text-green-400">{successLabel}</span>;
  }
  return null;
}

function ProviderAccountCard({
  provider,
  account,
  numbers,
  numbersLoading,
  readOnly,
  onError,
  onOpenRates,
}: {
  provider: ProviderName;
  account: ProviderAccount | undefined;
  numbers: NumberOut[];
  numbersLoading: boolean;
  readOnly: boolean;
  onError: (error: unknown) => void;
  onOpenRates: () => void;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const fields = PROVIDER_FIELDS[provider];
  const [label, setLabel] = React.useState(account?.label ?? "");
  const [credentials, setCredentials] = React.useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((field) => [
        field.name,
        field.secret ? "" : (account?.credentials[field.name] ?? ""),
      ]),
    ),
  );
  const [confirmingDisable, setConfirmingDisable] = React.useState(false);

  // A brand-new account has no server-side fallback for a blank field, so every field must
  // be filled in before Save is allowed (F6). An existing account can save partial changes -
  // buildCredentialsForSave drops blanks there instead (F5).
  const isCreate = !account;
  const missingFields = isCreate
    ? [
        ...(label.trim() === "" ? ["Label"] : []),
        ...fields
          .filter((field) => (credentials[field.name] ?? "").trim() === "")
          .map((field) => field.label),
      ]
    : [];
  const createIncomplete = isCreate && missingFields.length > 0;

  // F13: a save/probe/disable can change which carriers are DB-backed and live, so the
  // catalog + routing policy the health/policy sections read must be refreshed too.
  function invalidateRoutingQueries() {
    void queryClient.invalidateQueries({ queryKey: ["carrier-catalog"] });
    void queryClient.invalidateQueries({ queryKey: ["routing-policy"] });
  }

  // F1: never leave a just-submitted plaintext secret sitting in component state once the
  // server has it - the field's real value now only exists encrypted server-side.
  function clearSecretInputs() {
    setCredentials((prev) => {
      const next = { ...prev };
      for (const field of fields) {
        if (field.secret) next[field.name] = "";
      }
      return next;
    });
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (account) {
        const patch: PatchProviderAccountInput = {
          credentials: buildCredentialsForSave(provider, account, credentials),
        };
        // F4: the backend rejects a blank label outright (min_length=1) - only send it
        // when it is both non-blank and an actual change.
        if (label.trim() !== "" && label !== account.label) {
          patch.label = label;
        }
        return patchProviderAccount(api, account.id, patch);
      }
      return createProviderAccount(api, {
        provider,
        label,
        credentials: buildCredentialsForSave(provider, undefined, credentials),
      });
    },
    onSuccess: (saved) => {
      // The mutation response is the full, authoritative account record - write it straight
      // into the cache instead of also invalidating, which would trigger a redundant refetch
      // (and could race with it under a slow network).
      queryClient.setQueryData<ProviderAccount[]>(["provider-accounts"], (old) => {
        const list = old ?? [];
        if (account) {
          return list.map((item) => (item.id === saved.id ? saved : item));
        }
        return [...list, saved];
      });
      invalidateRoutingQueries();
      clearSecretInputs();
    },
    onError: (error) => onError(error),
  });

  const probeMutation = useMutation({
    mutationFn: () => {
      if (!account) return Promise.reject(new Error(`No ${provider} account to probe`));
      return probeProviderAccount(api, account.id);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<ProviderAccount[]>(["provider-accounts"], (old) =>
        (old ?? []).map((item) => (item.id === saved.id ? saved : item)),
      );
      invalidateRoutingQueries();
    },
    onError: (error) => onError(error),
  });

  const disableMutation = useMutation({
    mutationFn: () => {
      if (!account) return Promise.reject(new Error(`No ${provider} account to disable`));
      return disableProviderAccount(api, account.id);
    },
    onSuccess: () => {
      setConfirmingDisable(false);
      void queryClient.invalidateQueries({ queryKey: ["provider-accounts"] });
      invalidateRoutingQueries();
    },
    onError: (error) => onError(error),
  });

  const pill = accountStatusPill(account?.status);

  return (
    <section
      role="region"
      aria-label={`${provider} account`}
      className="space-y-4 rounded-md border border-neutral-800 bg-neutral-900 p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-neutral-50">{provider}</h3>
            <span className={cn("rounded-full px-2 py-0.5 text-xs", pill.className)}>
              {pill.label}
            </span>
          </div>
          {account?.last_probe_detail && (
            <p className="text-xs text-neutral-500">{account.last_probe_detail}</p>
          )}
        </div>
        {account && (
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={`Probe ${provider}`}
              disabled={readOnly || probeMutation.isPending}
              onClick={() => probeMutation.mutate()}
              className="border-neutral-700 bg-transparent px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800"
            >
              Probe
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={confirmingDisable ? `Confirm disable ${provider}` : `Disable ${provider}`}
              disabled={readOnly || disableMutation.isPending}
              onClick={() => {
                if (confirmingDisable) {
                  disableMutation.mutate();
                } else {
                  setConfirmingDisable(true);
                }
              }}
              className={cn(
                "border-neutral-700 bg-transparent px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800",
                confirmingDisable && "border-red-800 text-red-400 hover:bg-red-950",
              )}
            >
              {confirmingDisable ? "Confirm disable" : "Disable"}
            </Button>
          </div>
        )}
      </div>

      <form onSubmit={(e) => { e.preventDefault(); saveMutation.mutate(); }} className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={`${provider}-label`} className="block text-xs text-neutral-400">
            Label
          </label>
          <input
            id={`${provider}-label`}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            disabled={readOnly}
            aria-invalid={isCreate && label.trim() === ""}
            className="h-9 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 placeholder:text-neutral-500 aria-[invalid=true]:border-red-800"
          />
        </div>
        {fields.map((field) => (
          <div key={field.name} className="space-y-1">
            <label htmlFor={`${provider}-${field.name}`} className="block text-xs text-neutral-400">
              {field.label}
            </label>
            <input
              id={`${provider}-${field.name}`}
              type={field.secret ? "password" : "text"}
              value={credentials[field.name] ?? ""}
              onChange={(e) =>
                setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
              }
              placeholder={
                field.secret && account?.credentials[field.name] === SECRET_MASK
                  ? "stored — leave blank to keep"
                  : undefined
              }
              disabled={readOnly}
              aria-invalid={isCreate && (credentials[field.name] ?? "").trim() === ""}
              className="h-9 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 placeholder:text-neutral-500 aria-[invalid=true]:border-red-800"
            />
          </div>
        ))}
        {createIncomplete && (
          <p className="md:col-span-2 text-xs text-amber-400">
            Missing: {missingFields.join(", ")}
          </p>
        )}
        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <Button
            type="submit"
            size="sm"
            aria-label={`Save ${provider}`}
            disabled={readOnly || saveMutation.isPending || createIncomplete}
            className="bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 hover:opacity-90"
          >
            Save
          </Button>
          <MutationStatus mutation={saveMutation} pendingLabel="Saving…" successLabel="Saved" />
          <MutationStatus mutation={probeMutation} pendingLabel="Probing…" successLabel="Probed" />
          <MutationStatus mutation={disableMutation} pendingLabel="Disabling…" successLabel="Disabled" />
        </div>
      </form>

      <div className="border-t border-neutral-800 pt-3">
        <p className="text-xs font-medium text-neutral-400">Numbers on this provider</p>
        {numbersLoading ? (
          <p className="text-xs text-neutral-500">Loading numbers…</p>
        ) : numbers.length === 0 ? (
          <p className="text-xs text-neutral-500">None</p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-1">
            {numbers.map((number) => (
              <li
                key={number.id}
                className="rounded bg-neutral-800 px-2 py-0.5 text-xs text-neutral-200"
              >
                {formatPhone(number.e164)}
              </li>
            ))}
          </ul>
        )}
      </div>

      <SpendCard provider={provider} onOpenRates={onOpenRates} />
    </section>
  );
}

function statusPill(entry: CarrierCatalogOut): { label: string; className: string } {
  if (entry.live) return { label: "Live", className: "bg-green-950 text-green-400" };
  if (entry.enabled_flag === false) return { label: "Off", className: "bg-neutral-800 text-neutral-400" };
  return { label: "Needs credentials", className: "bg-amber-950 text-amber-400" };
}

function hasMms(entry: CarrierCatalogOut): boolean {
  const bytes = entry.capabilities?.max_media_bytes;
  return typeof bytes === "number" && bytes > 0;
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
    <li className="space-y-3 rounded-md border border-neutral-800 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-neutral-100">{entry.name}</span>
          <Badge className={pill.className}>{pill.label}</Badge>
          {entry.live && entry.state && (
            <Badge
              role={breakerLoud ? "alert" : undefined}
              className={cn(
                breakerLoud
                  ? "bg-red-950 font-semibold text-red-400"
                  : "bg-neutral-800 text-neutral-400",
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
          className="border-neutral-700 bg-transparent text-neutral-300 hover:bg-neutral-800"
        >
          {probing ? "Testing…" : "Test credentials"}
        </Button>
      </div>

      <div className="flex flex-wrap gap-1">
        <Badge className="bg-neutral-800 text-neutral-300">SMS</Badge>
        {hasMms(entry) && <Badge className="bg-neutral-800 text-neutral-300">MMS</Badge>}
        {entry.supports_voice && <Badge className="bg-neutral-800 text-neutral-300">Voice</Badge>}
        {entry.supports_numbers && <Badge className="bg-neutral-800 text-neutral-300">Numbers</Badge>}
        {entry.primary && <Badge className="bg-blue-950 text-blue-400">Primary</Badge>}
      </div>

      {!entry.live && (
        <div className="space-y-1 text-xs text-neutral-400">
          <p>{entry.reason}</p>
          {entry.missing.length > 0 && (
            <div className="space-y-1">
              <code className="block rounded bg-neutral-950 p-2 text-[11px] text-neutral-300">
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
        <p role="alert" className="text-xs text-red-400">
          {result.message}
        </p>
      )}
      {result && result.kind === "result" && result.data.ok && (
        <p className="text-xs text-green-400">{result.data.detail}</p>
      )}
      {result && result.kind === "result" && !result.data.ok && (
        <div className="space-y-1">
          <p role="alert" className="text-xs text-red-400">
            {result.data.detail}
          </p>
          <p className="break-all font-mono text-[10px] text-neutral-400">
            {result.data.checked}
          </p>
        </div>
      )}
    </li>
  );
}

function CarrierHealthSection({ api }: { api: ApiClient }) {
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
    <section className="space-y-4 rounded-md border border-neutral-800 p-4">
      <h2 className="text-base font-semibold text-neutral-50">Carrier health</h2>
      {isLoading ? (
        <Spinner />
      ) : sorted.length === 0 ? (
        <p className="text-sm text-neutral-400">No carriers found.</p>
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
    </section>
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
    <section className="space-y-4 rounded-md border border-neutral-800 p-4">
      <h2 className="text-base font-semibold text-neutral-50">Routing policy</h2>

      {error && (
        <p role="alert" className="text-sm text-red-400">
          {error}
        </p>
      )}

      {isLoading || !policy ? (
        <Spinner label="Loading routing policy" />
      ) : (
        <>
          {policy.preference.length === 0 ? (
            <p className="text-sm text-neutral-400">
              No carriers in the preference order yet.
            </p>
          ) : (
            <ol aria-label="Carrier preference order" className="max-w-md space-y-1">
              {policy.preference.map((name, i) => (
                <li
                  key={name}
                  className="flex items-center justify-between gap-2 rounded-md border border-neutral-800 px-3 py-1.5 text-sm text-neutral-100"
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

          <label className="flex items-center gap-2 text-sm text-neutral-200">
            <input
              type="checkbox"
              checked={policy.allow_intra_carrier_failover}
              onChange={(e) => patch({ allow_intra_carrier_failover: e.target.checked })}
              disabled={updatePolicy.isPending}
            />
            Allow intra-carrier failover
          </label>
          <label className="flex items-center gap-2 text-sm text-neutral-200">
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
    </section>
  );
}

export function ProvidersPage() {
  const { api, me, orgId } = useAuth();
  // F10: a 403 is still the authoritative backstop (custom roles, stale `me`), but the UI
  // should default to read-only for anyone who isn't owner/admin instead of only reacting
  // after a failed write.
  const [forcedReadOnly, setForcedReadOnly] = React.useState(false);
  const [storageError, setStorageError] = React.useState<string | null>(null);
  const [ratesOpen, setRatesOpen] = React.useState(false);

  const providerAccountsQuery = useQuery({
    queryKey: ["provider-accounts"],
    queryFn: () => fetchProviderAccounts(api),
  });
  const numbersQuery = useNumbers(api);

  const roleName = React.useMemo(
    () => me?.memberships.find((m) => m.org_id === orgId)?.role_name,
    [me, orgId],
  );
  const roleReadOnly = me != null && roleName !== "owner" && roleName !== "admin";
  const readOnly = roleReadOnly || forcedReadOnly;

  const handleError = React.useCallback((error: unknown) => {
    if (error instanceof ApiError) {
      if (error.status === 403) setForcedReadOnly(true);
      // F8: show whatever the backend actually said, verbatim - no substring gate. A 503
      // here always means the credential store (or some other account dependency) is down.
      if (error.status === 503) setStorageError(error.message);
    }
  }, []);

  React.useEffect(() => {
    if (providerAccountsQuery.error) handleError(providerAccountsQuery.error);
  }, [handleError, providerAccountsQuery.error]);

  const accountByProvider = React.useMemo(() => {
    const map = new Map<ProviderName, ProviderAccount>();
    (providerAccountsQuery.data ?? []).forEach((account) => map.set(account.provider, account));
    return map;
  }, [providerAccountsQuery.data]);

  return (
    <div className="dark mx-auto max-w-5xl space-y-8 bg-neutral-950 p-6 text-neutral-100">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-2">
          <h1 className="text-lg font-semibold text-neutral-50">Providers</h1>
          <p className="text-sm text-neutral-400">Provider accounts and carrier health.</p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setRatesOpen(true)}
          className="border-neutral-700 bg-transparent px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800"
        >
          Rates
        </Button>
      </div>

      {ratesOpen && (
        <RatesDrawer readOnly={readOnly} onClose={() => setRatesOpen(false)} />
      )}

      {storageError && (
        <div
          role="alert"
          className="rounded-md border border-red-800 bg-red-950 p-3 text-sm text-red-300"
        >
          {storageError}
        </div>
      )}
      {readOnly && (
        <p className="text-sm text-amber-400">
          Read-only: your role can view provider status but not edit credentials.
        </p>
      )}

      <section className="space-y-4">
        <h2 className="text-base font-semibold text-neutral-50">Provider accounts</h2>
        {providerAccountsQuery.isLoading ? (
          <p className="text-sm text-neutral-400">Loading provider accounts…</p>
        ) : providerAccountsQuery.isError ? (
          // F9: the list call itself failed (usually the 503 above) - every card's account
          // data is unknown, not "not configured", so render nothing that invites an edit
          // that will just fail the same way.
          <p className="text-sm text-neutral-400">Provider accounts are unavailable.</p>
        ) : (
          PROVIDER_NAMES.map((provider) => {
            const account = accountByProvider.get(provider);
            const numbers = (numbersQuery.data ?? []).filter(
              (number) => number.carrier === provider,
            );
            return (
              <ProviderAccountCard
                key={account?.id ?? `${provider}-none`}
                provider={provider}
                account={account}
                numbers={numbers}
                numbersLoading={numbersQuery.isLoading}
                readOnly={readOnly}
                onError={handleError}
                onOpenRates={() => setRatesOpen(true)}
              />
            );
          })
        )}
      </section>

      <CarrierHealthSection api={api} />
      <PolicySection api={api} />
    </div>
  );
}
