import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  API_KEY_SCOPE_CATALOGUE,
  PLATFORM_EVENT_TYPES,
  useApiKeys,
  useAuditLog,
  useCreateApiKey,
  useCreateWebhookEndpoint,
  useDeleteWebhookEndpoint,
  useReconciliation,
  useRedeliverWebhook,
  useRevokeApiKey,
  useRotateApiKey,
  useUpdateWebhookEndpoint,
  useUsage,
  useWebhookDeliveries,
  useWebhookEndpoints,
  type ApiKeyCreatedOut,
  type ApiKeyOut,
  type AuditEntryOut,
  type AuditFilters,
  type WebhookEndpointCreatedOut,
  type WebhookEndpointOut,
} from "@/api/hooks";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
} from "@/components/ui/primitives";
import { ConsoleCard, PageHeader, SurfaceCard } from "@/components/ui/consoleChrome";
import { cn } from "@/lib/utils";

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

function CopyOnceBox({
  label,
  value,
  note,
  onDismiss,
}: {
  label: string;
  value: string;
  note: string;
  onDismiss: () => void;
}) {
  return (
    <ConsoleCard className="space-y-[9px] text-[13.5px]">
      <p className="font-medium">{label}</p>
      <p className="text-xs text-[hsl(var(--cx-muted))]">{note}</p>
      <div className="flex gap-2">
        <Input
          readOnly
          aria-label={label}
          value={value}
          onFocus={(e) => e.currentTarget.select()}
        />
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            navigator.clipboard?.writeText(value).catch(() => {});
          }}
        >
          Copy
        </Button>
      </div>
      <Button type="button" size="sm" variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
    </ConsoleCard>
  );
}

function CheckboxGrid({
  options,
  selected,
  onToggle,
  legend,
}: {
  options: readonly string[];
  selected: Set<string>;
  onToggle: (value: string) => void;
  legend: string;
}) {
  return (
    <fieldset className="max-h-40 overflow-y-auto rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[11px]">
      <legend className="px-1 text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">{legend}</legend>
      <div className="grid grid-cols-2 gap-x-[12px] gap-y-[6px] sm:grid-cols-3">
        {options.map((opt) => (
          <label key={opt} className="flex items-center gap-[7px] text-[12px]">
            <input
              type="checkbox"
              checked={selected.has(opt)}
              onChange={() => onToggle(opt)}
              aria-label={opt}
            />
            {opt}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

// =====================================================================================
// API keys (DR-3)
// =====================================================================================
function apiKeyStatusTone(status: string): "success" | "warning" | "neutral" {
  if (status === "active") return "success";
  if (status === "revoked") return "neutral";
  return "warning";
}

function ApiKeysSection() {
  const { api } = useAuth();
  const { data: keys, isLoading, error: keysError, refetch: refetchKeys } = useApiKeys(api);
  const createKey = useCreateApiKey(api);
  const revokeKey = useRevokeApiKey(api);
  const rotateKey = useRotateApiKey(api);

  const [name, setName] = React.useState("");
  const [scopes, setScopes] = React.useState<Set<string>>(new Set());
  const [error, setError] = React.useState<unknown | null>(null);
  const [created, setCreated] = React.useState<ApiKeyCreatedOut | null>(null);

  function toggleScope(scope: string) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  }

  async function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await createKey.mutateAsync({ name, scopes: Array.from(scopes) });
      setCreated(result);
      setName("");
      setScopes(new Set());
    } catch (err) {
      setError(err);
    }
  }

  async function doRevoke(id: string) {
    setError(null);
    try {
      await revokeKey.mutateAsync(id);
    } catch (err) {
      setError(err);
    }
  }

  async function doRotate(id: string) {
    setError(null);
    try {
      const result = await rotateKey.mutateAsync(id);
      setCreated(result);
    } catch (err) {
      setError(err);
    }
  }

  return (
    <Section title="API keys" description="Create and manage API keys." className="space-y-4">
      {isLoading ? (
        <Spinner />
      ) : keysError ? (
        <div role="alert" className="space-y-2 text-sm text-[hsl(var(--cx-danger))]">
          <p>{(keysError as Error).message}</p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchKeys()}>
            Retry
          </Button>
        </div>
      ) : (keys ?? []).length === 0 ? (
        /* P20b: PlatformPage.test.tsx pins the exact empty-state text "No API keys yet." */
        <EmptyState
          title="No API keys yet."
          description="Create an API key to access the platform API."
        />
      ) : (
        <SurfaceCard className="overflow-x-auto p-0">
          <table className="w-full text-[13.5px]">
            <thead>
              <tr className="border-b border-[hsl(var(--cx-line))] text-left text-[11.5px] text-[hsl(var(--cx-muted))]">
                <th className="px-[12px] py-[10px] font-semibold">Name</th>
                <th className="px-[12px] py-[10px] font-semibold">Prefix</th>
                <th className="px-[12px] py-[10px] font-semibold">Scopes</th>
                <th className="px-[12px] py-[10px] font-semibold">Status</th>
                <th className="px-[12px] py-[10px] font-semibold">Last used</th>
                <th className="px-[12px] py-[10px] font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[hsl(var(--cx-line))]">
              {(keys ?? []).map((k: ApiKeyOut) => (
                <tr key={k.id}>
                  <td className="px-[12px] py-[10px]">{k.name}</td>
                  <td className="px-[12px] py-[10px] font-mono text-[11.5px]">{k.prefix}</td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {k.scopes.join(", ")}
                  </td>
                  <td className="px-[12px] py-[10px]">
                    <Pill tone={apiKeyStatusTone(k.status)}>{k.status}</Pill>
                  </td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "Never"}
                  </td>
                  <td className="px-[12px] py-[10px]">
                    {k.status === "active" && (
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => doRotate(k.id)}
                          disabled={rotateKey.isPending}
                        >
                          Rotate
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => doRevoke(k.id)}
                          disabled={revokeKey.isPending}
                        >
                          Revoke
                        </Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </SurfaceCard>
      )}

      <ConsoleCard className="space-y-[12px]">
        <form className="space-y-3" onSubmit={submitCreate}>
          <div className="flex items-end gap-2">
            <div className="flex-1 space-y-1">
              <label className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]" htmlFor="apikey-name">
                Name
              </label>
              <Input
                id="apikey-name"
                aria-label="Key name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <Button
              type="submit"
              disabled={!name.trim() || scopes.size === 0 || createKey.isPending}
            >
              Create key
            </Button>
          </div>
          <CheckboxGrid
            options={API_KEY_SCOPE_CATALOGUE}
            selected={scopes}
            onToggle={toggleScope}
            legend="Scopes"
          />
        </form>

        <MutationStatus error={error} />

        {created && (
          <CopyOnceBox
            label="API key"
            value={created.key}
            note="This key is shown once and cannot be retrieved again. If it is lost, revoke it and create a new one."
            onDismiss={() => setCreated(null)}
          />
        )}
      </ConsoleCard>
    </Section>
  );
}

// =====================================================================================
// Outbound webhooks (DR-4/DR-5)
// =====================================================================================
function endpointStatusTone(status: string): "success" | "neutral" {
  return status === "active" ? "success" : "neutral";
}

function deliveryStatusTone(status: string): "success" | "warning" | "danger" | "neutral" {
  switch (status) {
    case "delivered":
      return "success";
    case "pending":
      return "warning";
    case "dead":
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

function DeliveriesDrawer({ endpoint }: { endpoint: WebhookEndpointOut }) {
  const { api } = useAuth();
  const [status, setStatus] = React.useState("");
  const {
    data: deliveries,
    isLoading,
    error: deliveriesError,
    refetch: refetchDeliveries,
  } = useWebhookDeliveries(api, endpoint.id, status || undefined);
  const redeliver = useRedeliverWebhook(api);
  const [error, setError] = React.useState<unknown | null>(null);

  async function doRedeliver(id: string) {
    setError(null);
    try {
      await redeliver.mutateAsync(id);
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="space-y-[11px] border-t border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px]">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
          Deliveries for {endpoint.url}
        </h3>
        <Select
          aria-label="Filter deliveries by status"
          className="h-7 w-auto px-2 text-xs"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="delivered">Delivered</option>
          <option value="failed">Failed</option>
          <option value="dead">Dead</option>
        </Select>
      </div>

      <MutationStatus error={error} />

      {isLoading ? (
        <Spinner label="Loading deliveries" />
      ) : deliveriesError ? (
        <div role="alert" className="space-y-2 text-xs text-[hsl(var(--cx-danger))]">
          <p>{(deliveriesError as Error).message}</p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchDeliveries()}>
            Retry
          </Button>
        </div>
      ) : (deliveries ?? []).length === 0 ? (
        <EmptyState title="No deliveries yet." description="Delivered events will appear here." />
      ) : (
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-left text-[11.5px] text-[hsl(var(--cx-muted))]">
              <th className="px-[10px] py-[8px] font-semibold">Event</th>
              <th className="px-[10px] py-[8px] font-semibold">Status</th>
              <th className="px-[10px] py-[8px] font-semibold">Attempts</th>
              <th className="px-[10px] py-[8px] font-semibold">Last error</th>
              <th className="px-[10px] py-[8px] font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[hsl(var(--cx-line))]">
            {(deliveries ?? []).map((d) => (
              <tr key={d.id}>
                <td className="px-[10px] py-[8px]">{d.event_type}</td>
                <td className="px-[10px] py-[8px]">
                  <Pill tone={deliveryStatusTone(d.status)}>{d.status}</Pill>
                </td>
                <td className="px-[10px] py-[8px]">{d.attempts}</td>
                <td className="px-[10px] py-[8px]">
                  {d.last_status_code ? `HTTP ${d.last_status_code}` : ""}
                  {d.last_error ? ` ${d.last_error}` : ""}
                  {!d.last_status_code && !d.last_error ? "—" : ""}
                </td>
                <td className="px-[10px] py-[8px]">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => doRedeliver(d.id)}
                    disabled={redeliver.isPending}
                  >
                    Redeliver
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function WebhooksSection() {
  const { api } = useAuth();
  const {
    data: endpoints,
    isLoading,
    error: endpointsError,
    refetch: refetchEndpoints,
  } = useWebhookEndpoints(api);
  const createEndpoint = useCreateWebhookEndpoint(api);
  const updateEndpoint = useUpdateWebhookEndpoint(api);
  const deleteEndpoint = useDeleteWebhookEndpoint(api);

  const [url, setUrl] = React.useState("");
  const [eventTypes, setEventTypes] = React.useState<Set<string>>(new Set());
  const [error, setError] = React.useState<unknown | null>(null);
  const [created, setCreated] = React.useState<WebhookEndpointCreatedOut | null>(null);
  const [expandedId, setExpandedId] = React.useState<string | null>(null);

  function toggleEventType(t: string) {
    setEventTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }

  async function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await createEndpoint.mutateAsync({
        url,
        event_types: Array.from(eventTypes),
      });
      setCreated(result);
      setUrl("");
      setEventTypes(new Set());
    } catch (err) {
      setError(err);
    }
  }

  async function toggleStatus(endpoint: WebhookEndpointOut) {
    setError(null);
    try {
      await updateEndpoint.mutateAsync({
        endpointId: endpoint.id,
        status: endpoint.status === "active" ? "disabled" : "active",
      });
    } catch (err) {
      setError(err);
    }
  }

  async function doDelete(id: string) {
    setError(null);
    try {
      await deleteEndpoint.mutateAsync(id);
      if (expandedId === id) setExpandedId(null);
    } catch (err) {
      setError(err);
    }
  }

  return (
    <Section title="Callbacks" description="Receive platform events at a callback URL." className="space-y-4">
      {isLoading ? (
        <Spinner />
      ) : endpointsError ? (
        <div role="alert" className="space-y-2 text-sm text-[hsl(var(--cx-danger))]">
          <p>{(endpointsError as Error).message}</p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetchEndpoints()}>
            Retry
          </Button>
        </div>
      ) : (endpoints ?? []).length === 0 ? (
        /* P20b: PlatformPage.test.tsx pins the exact empty-state text "No webhook endpoints yet." */
        <EmptyState
          title="No webhook endpoints yet."
          description="Create a callback to receive platform events."
        />
      ) : (
        <div className="space-y-[11px]">
          {(endpoints ?? []).map((ep: WebhookEndpointOut) => (
            <ConsoleCard key={ep.id} className="overflow-hidden p-0">
              <div className="flex flex-wrap items-center justify-between gap-[11px] p-[14px]">
                <div className="min-w-0">
                  <p className="text-[13.5px] font-semibold">{ep.url}</p>
                  <p className="mt-[3px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {ep.event_types.join(", ")}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-[7px]">
                  <Pill tone={endpointStatusTone(ep.status)}>{ep.status}</Pill>
                  <span className="text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {ep.failure_streak} failing
                  </span>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setExpandedId(expandedId === ep.id ? null : ep.id)}
                  >
                    {expandedId === ep.id ? "Hide deliveries" : "Deliveries"}
                  </Button>
                  <Button type="button" size="sm" variant="outline" onClick={() => toggleStatus(ep)}>
                    {ep.status === "active" ? "Disable" : "Enable"}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="destructive"
                    onClick={() => doDelete(ep.id)}
                  >
                    Delete
                  </Button>
                </div>
              </div>
              {expandedId === ep.id && <DeliveriesDrawer endpoint={ep} />}
            </ConsoleCard>
          ))}
        </div>
      )}

      <ConsoleCard className="space-y-[12px]">
        <form className="space-y-3" onSubmit={submitCreate}>
          <div className="flex items-end gap-2">
            <div className="flex-1 space-y-1">
              {/* P20b: PlatformPage.test.tsx queries the exact label "Endpoint URL". */}
              <label className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]" htmlFor="webhook-url">
                Endpoint URL
              </label>
              <Input
                id="webhook-url"
                aria-label="Endpoint URL"
                placeholder="https://example.com/callbacks/csaas"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </div>
            {/* P20b: PlatformPage.test.tsx pins the exact button name "Create endpoint". */}
            <Button
              type="submit"
              disabled={!url.trim() || eventTypes.size === 0 || createEndpoint.isPending}
            >
              Create endpoint
            </Button>
          </div>
          <CheckboxGrid
            options={PLATFORM_EVENT_TYPES}
            selected={eventTypes}
            onToggle={toggleEventType}
            legend="Event types"
          />
        </form>

        <MutationStatus error={error} />

        {created && (
          <CopyOnceBox
            label="Callback signing secret"
            value={created.secret}
            note="This secret is shown once and cannot be retrieved again. It signs every delivery to this callback URL (X-Callback-Signature)."
            onDismiss={() => setCreated(null)}
          />
        )}
      </ConsoleCard>
    </Section>
  );
}

// =====================================================================================
// Audit log (DR-6)
// =====================================================================================
function AuditSection() {
  const { api } = useAuth();
  const [filters, setFilters] = React.useState<AuditFilters>({});
  const [actionInput, setActionInput] = React.useState("");
  const [targetTypeInput, setTargetTypeInput] = React.useState("");
  const [cursor, setCursor] = React.useState<string | null>(null);
  const { data, isLoading, error, refetch } = useAuditLog(api, filters, cursor);
  const [rows, setRows] = React.useState<AuditEntryOut[]>([]);

  React.useEffect(() => {
    if (!data) return;
    setRows((prev) => (cursor ? [...prev, ...data.items] : data.items));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  function applyFilters(e: React.FormEvent) {
    e.preventDefault();
    setFilters({
      action: actionInput.trim() || undefined,
      target_type: targetTypeInput.trim() || undefined,
    });
    setCursor(null);
    setRows([]);
  }

  return (
    <Section title="Audit log" description="Recent platform activity." className="space-y-4">
      <ConsoleCard>
        <form className="flex flex-wrap items-end gap-2" onSubmit={applyFilters}>
          <div className="space-y-1">
            <label className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]" htmlFor="audit-action">
              Action
            </label>
            <Input
              id="audit-action"
              aria-label="Filter by action"
              placeholder="apikey.created"
              value={actionInput}
              onChange={(e) => setActionInput(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]" htmlFor="audit-target-type">
              Target type
            </label>
            <Input
              id="audit-target-type"
              aria-label="Filter by target type"
              placeholder="api_key"
              value={targetTypeInput}
              onChange={(e) => setTargetTypeInput(e.target.value)}
            />
          </div>
          <Button type="submit" variant="outline">
            Apply filters
          </Button>
        </form>
      </ConsoleCard>

      {isLoading && rows.length === 0 ? (
        <Spinner />
      ) : error ? (
        <div role="alert" className="space-y-2 text-sm text-[hsl(var(--cx-danger))]">
          <p>{(error as Error).message}</p>
          <Button type="button" size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title="No audit entries yet." description="Audit activity will appear here." />
      ) : (
        <SurfaceCard className="overflow-x-auto p-0">
          <table className="w-full text-[13.5px]">
            <thead>
              <tr className="border-b border-[hsl(var(--cx-line))] text-left text-[11.5px] text-[hsl(var(--cx-muted))]">
                <th className="px-[12px] py-[10px] font-semibold">Action</th>
                <th className="px-[12px] py-[10px] font-semibold">Target</th>
                <th className="px-[12px] py-[10px] font-semibold">Actor</th>
                <th className="px-[12px] py-[10px] font-semibold">When</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[hsl(var(--cx-line))]">
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="px-[12px] py-[10px] font-mono text-[11.5px]">{r.action}</td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {r.target_type}
                    {r.target_id ? ` (${r.target_id.slice(0, 8)})` : ""}
                  </td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {r.actor_api_key_id
                      ? `API key ${r.actor_api_key_id.slice(0, 8)}`
                      : r.actor_user_id
                        ? `User ${r.actor_user_id.slice(0, 8)}`
                        : "—"}
                  </td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </SurfaceCard>
      )}

      {data?.next_cursor && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => setCursor(data.next_cursor)}
          disabled={isLoading}
        >
          Load more
        </Button>
      )}
    </Section>
  );
}

// =====================================================================================
// Usage + reconciliation (DR-2)
// =====================================================================================
function verdictTone(verdict: string): "success" | "danger" | "neutral" {
  switch (verdict) {
    case "within_tolerance":
      return "success";
    case "mismatch":
      return "danger";
    default:
      return "neutral";
  }
}

function UsageSection() {
  const { api } = useAuth();
  const [date, setDate] = React.useState(todayUtc());
  const {
    data: usage,
    isLoading: usageLoading,
    error: usageError,
    refetch: refetchUsage,
  } = useUsage(api, date, date);
  const {
    data: reconciliation,
    isLoading: reconLoading,
    error: reconError,
    refetch: refetchRecon,
  } = useReconciliation(api, date);
  const usageOrReconError = usageError ?? reconError;

  return (
    <Section
      title="Usage"
      description="Daily usage and provider reconciliation."
      className="space-y-4"
      actions={
        <div className="space-y-1">
          <label className="sr-only" htmlFor="usage-date">
            Date
          </label>
          <Input
            id="usage-date"
            aria-label="Usage date"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </div>
      }
    >
      {usageLoading || reconLoading ? (
        <Spinner />
      ) : usageOrReconError ? (
        <div role="alert" className="space-y-2 text-sm text-[hsl(var(--cx-danger))]">
          <p>{(usageOrReconError as Error).message}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              refetchUsage();
              refetchRecon();
            }}
          >
            Retry
          </Button>
        </div>
      ) : (
        <SurfaceCard className="overflow-x-auto p-0">
          <table className="w-full text-[13.5px]">
            <thead>
              <tr className="border-b border-[hsl(var(--cx-line))] text-left text-[11.5px] text-[hsl(var(--cx-muted))]">
                <th className="px-[12px] py-[10px] font-semibold">Metric</th>
                <th className="px-[12px] py-[10px] font-semibold">Ours</th>
                <th className="px-[12px] py-[10px] font-semibold">Provider</th>
                <th className="px-[12px] py-[10px] font-semibold">Verdict</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[hsl(var(--cx-line))]">
              {(reconciliation?.items ?? []).map((item) => (
                <tr key={item.metric}>
                  <td className="px-[12px] py-[10px] font-mono text-[11.5px]">{item.metric}</td>
                  <td className="px-[12px] py-[10px]">{item.ours}</td>
                  <td className="px-[12px] py-[10px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                    {item.carrier ?? "—"}
                  </td>
                  <td className="px-[12px] py-[10px]">
                    <Pill tone={verdictTone(item.verdict)}>{item.verdict}</Pill>
                  </td>
                </tr>
              ))}
              {(reconciliation?.items ?? []).length === 0 && (usage ?? []).length === 0 && (
                <tr>
                  <td className="px-[12px] py-[18px] text-center text-[12px] text-[hsl(var(--cx-muted))]" colSpan={4}>
                    No usage recorded for this date yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </SurfaceCard>
      )}
    </Section>
  );
}

export function PlatformPage() {
  return (
    <div className={cn("mx-auto max-w-4xl space-y-[18px]")}>
      <PageHeader
        title="Platform"
        description="Keys, callbacks, the audit trail and what you used."
      />
      <ApiKeysSection />
      <WebhooksSection />
      <AuditSection />
      <UsageSection />
      {/* PlatformBillingOps was mounted here: a platform-operator billing panel sitting at the
          bottom of a CUSTOMER's Developers tab. Removed, for three reasons.
          1. It authenticates by pasting the shared X-Platform-Ops-Token into a text box and
             keeping it in sessionStorage. The named-operator console requires a second factor
             and a recent step-up; this was the weaker of the two doors, on the more public page.
          2. It is built against a contract the server does not serve: it GETs
             /platform/billing/rates (PUT only - platform.py:636), sends `cost_micros` where the
             API takes `unit_cost_micros` (:481), and reads `org_id`/`name` off a response that
             carries neither (_platform_billing_shape, :488). Its 11 tests pass because they stub
             the shape the component imagines, so they compare it against itself.
          3. Operator billing now lives in the Ops console (components/ops/BillingTab.tsx),
             behind require_operator, written against the contract read out of the Python.
          The component and its test file still exist and are now unreferenced - delete them once
          the Ops console Billing tab has been exercised against a live backend. */}
    </div>
  );
}
