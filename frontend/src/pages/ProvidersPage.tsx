import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { ApiError, type ApiClient } from "@/api/client";
import {
  useCarrierCatalog,
  useNumbers,
  useProbeCarrier,
  useRoutingPolicy,
  useUpdateRoutingPolicy,
  type CarrierCatalogOut,
  type ProbeOut,
  type RoutingPolicyIn,
} from "@/api/hooks";
import {
  PROVIDER_FIELDS,
  PROVIDER_LABELS,
  PROVIDER_NAMES,
  SECRET_MASK,
  createProviderAccount,
  disableProviderAccount,
  fetchProviderAccounts,
  formatSpendMtd,
  patchProviderAccount,
  probeProviderAccount,
  type PatchProviderAccountInput,
  type ProviderAccount,
  type ProviderAccountStatus,
  type ProviderName,
} from "@/api/providers";
import {
  Badge,
  Button,
  Card,
  Collapsible,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import { RatesDrawer } from "@/components/spend/RatesDrawer";
import { SpendCard } from "@/components/spend/SpendCard";

type ProbeState = { kind: "result"; data: ProbeOut } | { kind: "error"; message: string };

function providerDisplayName(name: string): string {
  return PROVIDER_LABELS[name as ProviderName] ?? name;
}

function accountStatusPill(status: ProviderAccountStatus): { label: string; tone: PillTone } {
  switch (status) {
    case "unverified":
      return { label: "Not checked yet", tone: "neutral" };
    case "active":
      return { label: "Working", tone: "success" };
    case "failed":
      return { label: "Not working", tone: "danger" };
    case "disabled":
      return { label: "Turned off", tone: "neutral" };
    default:
      return { label: "Not configured", tone: "neutral" };
  }
}

function carrierStatusPill(entry: CarrierCatalogOut): { label: string; tone: PillTone } {
  if (entry.live) return { label: "Live", tone: "success" };
  if (entry.enabled_flag === false) return { label: "Off", tone: "neutral" };
  return { label: "Needs credentials", tone: "warning" };
}

function providerStatePill(state?: string): { label: string; tone: PillTone } | null {
  if (!state) return null;
  if (state === "closed") return { label: "Healthy", tone: "success" };
  if (state === "open") return { label: "Not delivering right now", tone: "danger" };
  return { label: "Recovering", tone: "warning" };
}

function hasMms(entry: CarrierCatalogOut): boolean {
  const bytes = entry.capabilities?.max_media_bytes;
  return typeof bytes === "number" && bytes > 0;
}

function numbersCountText(count: number): string {
  if (count === 0) return "No numbers";
  if (count === 1) return "1 number";
  return `${count} numbers`;
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

function ProviderAccountCard({
  provider,
  account,
  readOnly,
  onError,
  onOpenRates,
}: {
  provider: ProviderName;
  account: ProviderAccount;
  readOnly: boolean;
  onError: (error: unknown) => void;
  onOpenRates: () => void;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const fields = PROVIDER_FIELDS[provider];
  const [label, setLabel] = React.useState(account.label);
  const [credentials, setCredentials] = React.useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((field) => [
        field.name,
        field.secret ? "" : (account.credentials[field.name] ?? ""),
      ]),
    ),
  );
  const [confirmingDisable, setConfirmingDisable] = React.useState(false);
  // F45: only the most recently triggered mutation's status is shown - otherwise a stale
  // isSuccess from an earlier action (e.g. probe) stays visible forever alongside a newer one.
  const [lastAction, setLastAction] = React.useState<"save" | "probe" | "disable" | null>(null);

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
      const patch: PatchProviderAccountInput = {
        credentials: buildCredentialsForSave(provider, account, credentials),
      };
      // F4: the backend rejects a blank label outright (min_length=1) - only send it
      // when it is both non-blank and an actual change.
      if (label.trim() !== "" && label.trim() !== account.label) {
        patch.label = label.trim();
      }
      return patchProviderAccount(api, account.id, patch);
    },
    onSuccess: (saved) => {
      // The mutation response is the full, authoritative account record - write it straight
      // into the cache instead of also invalidating, which would trigger a redundant refetch
      // (and could race with it under a slow network).
      queryClient.setQueryData<ProviderAccount[]>(["provider-accounts"], (old) => {
        const list = old ?? [];
        return list.map((item) => (item.id === saved.id ? saved : item));
      });
      invalidateRoutingQueries();
      clearSecretInputs();
      setLabel(saved.label);
    },
    onError: (error) => onError(error),
  });

  const probeMutation = useMutation({
    mutationFn: () => probeProviderAccount(api, account.id),
    onSuccess: (saved) => {
      queryClient.setQueryData<ProviderAccount[]>(["provider-accounts"], (old) =>
        (old ?? []).map((item) => (item.id === saved.id ? saved : item)),
      );
      invalidateRoutingQueries();
    },
    onError: (error) => onError(error),
  });

  const disableMutation = useMutation({
    mutationFn: () => disableProviderAccount(api, account.id),
    onSuccess: () => {
      setConfirmingDisable(false);
      void queryClient.invalidateQueries({ queryKey: ["provider-accounts"] });
      invalidateRoutingQueries();
    },
    onError: (error) => onError(error),
  });

  const pill = accountStatusPill(account.status);
  const providerName = providerDisplayName(provider);

  return (
    /* A named group per connection: several providers share credential field labels
       ("Auth token" is both Twilio's and Plivo's), so without a landmark every
       getByLabelText across the page is ambiguous - and a screen-reader user has no
       way to tell which connection a field belongs to either. */
    <Card role="group" aria-label={`${account.label} connection`} className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">{account.label}</h3>
            <Pill tone={pill.tone}>{pill.label}</Pill>
          </div>
          <p className="text-xs text-muted-foreground">{providerName}</p>
          {account.last_probe_detail && (
            <p className="text-xs text-muted-foreground">{account.last_probe_detail}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Test ${providerName}`}
            disabled={readOnly || probeMutation.isPending}
            onClick={() => {
              setLastAction("probe");
              probeMutation.mutate();
            }}
          >
            Test
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={confirmingDisable ? `Confirm disable ${providerName}` : `Disable ${providerName}`}
            disabled={readOnly || disableMutation.isPending}
            onClick={() => {
              if (confirmingDisable) {
                setLastAction("disable");
                disableMutation.mutate();
              } else {
                setConfirmingDisable(true);
              }
            }}
          >
            {confirmingDisable ? "Confirm disable" : "Disable"}
          </Button>
        </div>
      </div>

      <p className="text-sm text-muted-foreground">{numbersCountText(account.numbers_count)}</p>
      <p className="text-sm text-muted-foreground">
        Spend this month: {formatSpendMtd(account.spend_mtd_micros)}
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          setLastAction("save");
          saveMutation.mutate();
        }}
        className="grid gap-3 md:grid-cols-2"
      >
        <div className="space-y-1">
          <label htmlFor={`${provider}-label`} className="block text-xs text-muted-foreground">
            Label
          </label>
          <Input
            id={`${provider}-label`}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            disabled={readOnly}
          />
        </div>
        {fields.map((field) => (
          <div key={field.name} className="space-y-1">
            <label htmlFor={`${provider}-${field.name}`} className="block text-xs text-muted-foreground">
              {field.label}
            </label>
            <Input
              id={`${provider}-${field.name}`}
              type={field.secret ? "password" : "text"}
              value={credentials[field.name] ?? ""}
              onChange={(e) =>
                setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
              }
              placeholder={
                field.secret && account.credentials[field.name] === SECRET_MASK
                  ? "stored — leave blank to keep"
                  : undefined
              }
              disabled={readOnly}
            />
          </div>
        ))}
        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <Button type="submit" size="sm" disabled={readOnly || saveMutation.isPending}>
            Save
          </Button>
          {lastAction === "save" && (
            <MutationStatus
              pending={saveMutation.isPending}
              error={saveMutation.error}
              success="Saved"
              pendingLabel="Saving…"
            />
          )}
          {lastAction === "probe" && (
            <MutationStatus
              pending={probeMutation.isPending}
              error={probeMutation.error}
              success="Tested"
              pendingLabel="Testing…"
            />
          )}
          {lastAction === "disable" && (
            <MutationStatus
              pending={disableMutation.isPending}
              error={disableMutation.error}
              success="Disabled"
              pendingLabel="Disabling…"
            />
          )}
        </div>
      </form>

      <SpendCard provider={provider} onOpenRates={onOpenRates} />
    </Card>
  );
}

function CarrierCard({
  entry,
  onProbe,
  probing,
  result,
  readOnly,
}: {
  entry: CarrierCatalogOut;
  onProbe: () => void;
  probing: boolean;
  result: ProbeState | undefined;
  readOnly: boolean;
}) {
  const pill = carrierStatusPill(entry);
  const statePill = entry.live ? providerStatePill(entry.state) : null;
  const circuitLoud = statePill !== null && entry.state !== "closed";

  return (
    <li className="space-y-3 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-foreground">
            {providerDisplayName(entry.name)}
          </span>
          <Pill tone={pill.tone}>{pill.label}</Pill>
          {statePill && (
            <Pill tone={statePill.tone} role={circuitLoud ? "alert" : undefined}>
              {statePill.label}
            </Pill>
          )}
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onProbe}
          disabled={entry.missing.length > 0 || probing || readOnly}
        >
          {probing ? "Testing…" : "Test credentials"}
        </Button>
      </div>

      <div className="flex flex-wrap gap-1">
        <Badge className="bg-muted text-muted-foreground">SMS</Badge>
        {hasMms(entry) && <Badge className="bg-muted text-muted-foreground">MMS</Badge>}
        {entry.supports_voice && <Badge className="bg-muted text-muted-foreground">Voice</Badge>}
        {entry.supports_numbers && <Badge className="bg-muted text-muted-foreground">Numbers</Badge>}
        {entry.primary && <Badge className="bg-muted text-muted-foreground">Primary</Badge>}
      </div>

      {!entry.live && (
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>{entry.reason}</p>
          {entry.missing.length > 0 && (
            <div className="space-y-1">
              <code className="block rounded bg-muted p-2 text-[11px] text-foreground">
                {entry.missing.map((name) => (
                  <div key={name}>{name}</div>
                ))}
              </code>
              <p>Ask your administrator to add these connection settings on the server, then restart it.</p>
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
        <p className="text-xs text-muted-foreground">{result.data.detail}</p>
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

function CarrierHealthSection({ api, readOnly }: { api: ApiClient; readOnly: boolean }) {
  const { data: catalog, isLoading, isError, error, refetch } = useCarrierCatalog(api);
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
    <section className="space-y-4 rounded-lg border border-border p-4">
      <h2 className="text-base font-semibold text-foreground">Provider health</h2>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div role="alert" className="space-y-2 text-sm text-destructive">
          <p>{(error as Error).message}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      ) : sorted.length === 0 ? (
        <p className="text-sm text-muted-foreground">No providers found.</p>
      ) : (
        <ul aria-label="Providers" className="space-y-3">
          {sorted.map((entry) => (
            <CarrierCard
              key={entry.name}
              entry={entry}
              onProbe={() => probe(entry.name)}
              probing={probing === entry.name}
              result={results[entry.name]}
              readOnly={readOnly}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function PolicySection({ api, readOnly }: { api: ApiClient; readOnly: boolean }) {
  const { data: policy, isLoading, isError, error: queryError, refetch } = useRoutingPolicy(api);
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
    <section className="space-y-4 rounded-lg border border-border p-4">
      <h2 className="text-base font-semibold text-foreground">Delivery preferences</h2>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {isError ? (
        <div role="alert" className="space-y-2 text-sm text-destructive">
          <p>{(queryError as Error).message}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      ) : isLoading || !policy ? (
        <Spinner label="Loading delivery preferences" />
      ) : (
        <>
          {policy.preference.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No providers in the preference order yet.
            </p>
          ) : (
            <ol aria-label="Provider preference order" className="max-w-md space-y-1">
              {policy.preference.map((name, i) => (
                <li
                  key={name}
                  className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-1.5 text-sm text-foreground"
                >
                  <span>
                    {i + 1}. {providerDisplayName(name)}
                  </span>
                  <div className="flex gap-1">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${providerDisplayName(name)} up`}
                      onClick={() => move(name, -1)}
                      disabled={i === 0 || updatePolicy.isPending || readOnly}
                    >
                      ↑
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${providerDisplayName(name)} down`}
                      onClick={() => move(name, 1)}
                      disabled={i === policy.preference.length - 1 || updatePolicy.isPending || readOnly}
                    >
                      ↓
                    </Button>
                  </div>
                </li>
              ))}
            </ol>
          )}

          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={policy.allow_intra_carrier_failover}
              onChange={(e) => patch({ allow_intra_carrier_failover: e.target.checked })}
              disabled={updatePolicy.isPending || readOnly}
            />
            Allow intra-provider failover
          </label>
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={policy.allow_cross_carrier_failover}
              onChange={(e) => patch({ allow_cross_carrier_failover: e.target.checked })}
              disabled={updatePolicy.isPending || readOnly}
            />
            Allow cross-provider failover
          </label>
        </>
      )}
    </section>
  );
}

function ConnectProviderSection({
  api,
  availableProviders,
  isLoading,
  readOnly,
  onError,
}: {
  api: ApiClient;
  availableProviders: ProviderName[];
  isLoading: boolean;
  readOnly: boolean;
  onError: (error: unknown) => void;
}) {
  const queryClient = useQueryClient();
  const [selectedProvider, setSelectedProvider] = React.useState<ProviderName | "">("");
  const [label, setLabel] = React.useState("");
  const [credentials, setCredentials] = React.useState<Record<string, string>>({});

  function handleProviderChange(value: string) {
    const next = value as ProviderName | "";
    setSelectedProvider(next);
    if (next) {
      const fields = PROVIDER_FIELDS[next];
      setCredentials(Object.fromEntries(fields.map((field) => [field.name, ""])));
    } else {
      setCredentials({});
    }
  }

  const createMutation = useMutation({
    mutationFn: async () => {
      if (!selectedProvider) return Promise.reject(new Error("Choose a provider first."));
      return createProviderAccount(api, {
        provider: selectedProvider,
        label: label.trim(),
        credentials: buildCredentialsForSave(selectedProvider, undefined, credentials),
      });
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<ProviderAccount[]>(["provider-accounts"], (old) => [
        ...(old ?? []),
        saved,
      ]);
      void queryClient.invalidateQueries({ queryKey: ["carrier-catalog"] });
      void queryClient.invalidateQueries({ queryKey: ["routing-policy"] });
      setSelectedProvider("");
      setLabel("");
      setCredentials({});
    },
    onError: (error) => onError(error),
  });

  if (isLoading) {
    return <Spinner label="Loading providers" />;
  }

  if (availableProviders.length === 0) {
    return <p className="text-sm text-muted-foreground">All providers are connected.</p>;
  }

  const fields = selectedProvider ? PROVIDER_FIELDS[selectedProvider] : [];
  // A brand-new account has no server-side fallback for a blank field, so every field must
  // be filled in before Save is allowed (F6). An existing account can save partial changes -
  // buildCredentialsForSave drops blanks there instead (F5).
  const missingFields = [
    ...(selectedProvider ? [] : ["Provider"]),
    ...(label.trim() === "" ? ["Label"] : []),
    ...fields
      .filter((field) => (credentials[field.name] ?? "").trim() === "")
      .map((field) => field.label),
  ];
  const createIncomplete = missingFields.length > 0;

  return (
    <div className="space-y-3">
      <Select
        aria-label="Connect a provider"
        value={selectedProvider}
        onChange={(e) => handleProviderChange(e.target.value)}
        disabled={readOnly}
      >
        <option value="">Choose a provider…</option>
        {availableProviders.map((provider) => (
          <option key={provider} value={provider}>
            {providerDisplayName(provider)}
          </option>
        ))}
      </Select>

      {selectedProvider && (
        <Card className="space-y-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              createMutation.mutate();
            }}
            className="grid gap-3 md:grid-cols-2"
          >
            <div className="space-y-1">
              <label htmlFor="connect-provider-label" className="block text-xs text-muted-foreground">
                Name this connection
              </label>
              <Input
                id="connect-provider-label"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                disabled={readOnly}
                aria-invalid={label.trim() === ""}
              />
            </div>
            {fields.map((field) => (
              <div key={field.name} className="space-y-1">
                <label htmlFor={`connect-${field.name}`} className="block text-xs text-muted-foreground">
                  {field.label}
                </label>
                <Input
                  id={`connect-${field.name}`}
                  type={field.secret ? "password" : "text"}
                  value={credentials[field.name] ?? ""}
                  onChange={(e) =>
                    setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                  }
                  disabled={readOnly}
                  aria-invalid={(credentials[field.name] ?? "").trim() === ""}
                />
              </div>
            ))}
            {createIncomplete && (
              <p className="md:col-span-2 text-xs text-destructive">
                Missing: {missingFields.join(", ")}
              </p>
            )}
            <div className="md:col-span-2 flex flex-wrap items-center gap-3">
              <Button
                type="submit"
                size="sm"
                disabled={readOnly || createMutation.isPending || createIncomplete}
              >
                Save
              </Button>
              <MutationStatus
                pending={createMutation.isPending}
                error={createMutation.error}
                success="Saved"
                pendingLabel="Saving…"
              />
            </div>
          </form>
        </Card>
      )}
    </div>
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

  const availableProviders = React.useMemo(() => {
    const connected = new Set((providerAccountsQuery.data ?? []).map((account) => account.provider));
    return PROVIDER_NAMES.filter((provider) => !connected.has(provider));
  }, [providerAccountsQuery.data]);

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

  // `isError`/`isLoading` do not narrow `data` for TypeScript - name the fallback once
  // rather than sprinkling non-null assertions through the JSX.
  const accounts = providerAccountsQuery.data ?? [];

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6 text-foreground">
      {/* The page header is a heading + one action, not a Section: `Section` requires
          children and an empty one would render a stray, unlabelled container. */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold">Providers</h1>
          <p className="text-sm text-muted-foreground">
            Connect the account that sends your messages and places your calls.
          </p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => setRatesOpen(true)}>
          Rates
        </Button>
      </div>

      {ratesOpen && <RatesDrawer readOnly={readOnly} onClose={() => setRatesOpen(false)} />}

      {storageError && (
        <div
          role="alert"
          className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
        >
          {storageError}
        </div>
      )}
      {numbersQuery.isError && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
        >
          <span>{(numbersQuery.error as Error).message}</span>
          <Button type="button" variant="outline" size="sm" onClick={() => numbersQuery.refetch()}>
            Retry
          </Button>
        </div>
      )}
      {readOnly && (
        <p className="text-sm text-destructive">
          Read-only: your role can view provider status but not edit credentials.
        </p>
      )}

      {/* No heading here on purpose. `Section` labels its <section> with the heading, so a
          heading reading "Connect a provider" would give the landmark the SAME accessible
          name as the picker inside it - every getByLabelText("Connect a provider") then
          matches two elements, and a screen reader announces the phrase twice in a row.
          The dropdown is the control the plan names, so the name lives on the dropdown. */}
      <div id="connect-provider">
        <ConnectProviderSection
          api={api}
          availableProviders={availableProviders}
          isLoading={providerAccountsQuery.isLoading}
          readOnly={readOnly}
          onError={handleError}
        />
      </div>

      <Section title="Connected providers">
        {providerAccountsQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading provider connections…</p>
        ) : providerAccountsQuery.isError ? (
          // F9: the list call itself failed (usually the 503 above) - every card's account
          // data is unknown, not "not configured", so render nothing that invites an edit
          // that will just fail the same way.
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>Provider accounts are unavailable.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => providerAccountsQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : accounts.length === 0 ? (
          /* No CTA: the "Connect a provider" picker is the section directly above this
             one, so a button repeating that exact phrase adds nothing and makes every
             getByText("Connect a provider") in the suite ambiguous. */
          <EmptyState
            title="No provider connections yet"
            description="Use the Connect a provider picker above to add your first one."
          />
        ) : (
          accounts.map((account) => (
            <ProviderAccountCard
              key={account.id}
              provider={account.provider}
              account={account}
              readOnly={readOnly}
              onError={handleError}
              onOpenRates={() => setRatesOpen(true)}
            />
          ))
        )}
      </Section>

      <Collapsible storageKey="settings.providers.advanced" defaultOpen={false} title="Advanced">
        <div className="space-y-4">
          <CarrierHealthSection api={api} readOnly={readOnly} />
          <PolicySection api={api} readOnly={readOnly} />
        </div>
      </Collapsible>
    </div>
  );
}
