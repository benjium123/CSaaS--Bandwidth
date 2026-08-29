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
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
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
    <div className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm">
      <p className="font-medium">{label}</p>
      <p className="text-xs text-muted-foreground">{note}</p>
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
    </div>
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
    <fieldset className="max-h-40 overflow-y-auto rounded-md border border-border p-2">
      <legend className="px-1 text-xs text-muted-foreground">{legend}</legend>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-3">
        {options.map((opt) => (
          <label key={opt} className="flex items-center gap-1.5 text-xs">
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
function apiKeyStatusBadgeClass(status: string): string {
  switch (status) {
    case "active":
      return "bg-green-100 text-green-800";
    case "revoked":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-amber-100 text-amber-800";
  }
}

function ApiKeysSection() {
  const { api } = useAuth();
  const { data: keys, isLoading } = useApiKeys(api);
  const createKey = useCreateApiKey(api);
  const revokeKey = useRevokeApiKey(api);
  const rotateKey = useRotateApiKey(api);

  const [name, setName] = React.useState("");
  const [scopes, setScopes] = React.useState<Set<string>>(new Set());
  const [error, setError] = React.useState<string | null>(null);
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
      setError((err as Error).message);
    }
  }

  async function doRevoke(id: string) {
    setError(null);
    try {
      await revokeKey.mutateAsync(id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function doRotate(id: string) {
    setError(null);
    try {
      const result = await rotateKey.mutateAsync(id);
      setCreated(result);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold">API keys</h2>

      {isLoading ? (
        <Spinner />
      ) : (keys ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No API keys yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Prefix</th>
                <th className="px-3 py-2 font-medium">Scopes</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Last used</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(keys ?? []).map((k: ApiKeyOut) => (
                <tr key={k.id}>
                  <td className="px-3 py-2">{k.name}</td>
                  <td className="px-3 py-2 font-mono text-xs">{k.prefix}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {k.scopes.join(", ")}
                  </td>
                  <td className="px-3 py-2">
                    <Badge className={apiKeyStatusBadgeClass(k.status)}>{k.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "Never"}
                  </td>
                  <td className="px-3 py-2">
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
        </div>
      )}

      <form className="space-y-3" onSubmit={submitCreate}>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="apikey-name">
              Name
            </label>
            <Input
              id="apikey-name"
              aria-label="Key name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <Button type="submit" disabled={!name.trim() || scopes.size === 0 || createKey.isPending}>
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

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {created && (
        <CopyOnceBox
          label="API key"
          value={created.key}
          note="This key is shown once and cannot be retrieved again. If it is lost, revoke it and create a new one."
          onDismiss={() => setCreated(null)}
        />
      )}
    </section>
  );
}

// =====================================================================================
// Outbound webhooks (DR-4/DR-5)
// =====================================================================================
function endpointStatusBadgeClass(status: string): string {
  return status === "active" ? "bg-green-100 text-green-800" : "bg-gray-100 text-gray-600";
}

function deliveryStatusBadgeClass(status: string): string {
  switch (status) {
    case "delivered":
      return "bg-green-100 text-green-800";
    case "pending":
      return "bg-amber-100 text-amber-800";
    case "dead":
    case "failed":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

function DeliveriesDrawer({ endpoint }: { endpoint: WebhookEndpointOut }) {
  const { api } = useAuth();
  const [status, setStatus] = React.useState("");
  const { data: deliveries, isLoading } = useWebhookDeliveries(
    api,
    endpoint.id,
    status || undefined,
  );
  const redeliver = useRedeliverWebhook(api);
  const [error, setError] = React.useState<string | null>(null);

  async function doRedeliver(id: string) {
    setError(null);
    try {
      await redeliver.mutateAsync(id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="space-y-2 border-t border-border bg-muted/40 p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium text-foreground">Deliveries for {endpoint.url}</h3>
        <select
          aria-label="Filter deliveries by status"
          className="h-7 rounded-md border border-border bg-background px-2 text-xs"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="delivered">Delivered</option>
          <option value="failed">Failed</option>
          <option value="dead">Dead</option>
        </select>
      </div>

      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}

      {isLoading ? (
        <Spinner label="Loading deliveries" />
      ) : (deliveries ?? []).length === 0 ? (
        <p className="text-xs text-muted-foreground">No deliveries yet.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="px-2 py-1 font-medium">Event</th>
              <th className="px-2 py-1 font-medium">Status</th>
              <th className="px-2 py-1 font-medium">Attempts</th>
              <th className="px-2 py-1 font-medium">Last error</th>
              <th className="px-2 py-1 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {(deliveries ?? []).map((d) => (
              <tr key={d.id}>
                <td className="px-2 py-1">{d.event_type}</td>
                <td className="px-2 py-1">
                  <Badge className={deliveryStatusBadgeClass(d.status)}>{d.status}</Badge>
                </td>
                <td className="px-2 py-1">{d.attempts}</td>
                <td className="px-2 py-1">
                  {d.last_status_code ? `HTTP ${d.last_status_code}` : ""}
                  {d.last_error ? ` ${d.last_error}` : ""}
                  {!d.last_status_code && !d.last_error ? "—" : ""}
                </td>
                <td className="px-2 py-1">
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
  const { data: endpoints, isLoading } = useWebhookEndpoints(api);
  const createEndpoint = useCreateWebhookEndpoint(api);
  const updateEndpoint = useUpdateWebhookEndpoint(api);
  const deleteEndpoint = useDeleteWebhookEndpoint(api);

  const [url, setUrl] = React.useState("");
  const [eventTypes, setEventTypes] = React.useState<Set<string>>(new Set());
  const [error, setError] = React.useState<string | null>(null);
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
      setError((err as Error).message);
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
      setError((err as Error).message);
    }
  }

  async function doDelete(id: string) {
    setError(null);
    try {
      await deleteEndpoint.mutateAsync(id);
      if (expandedId === id) setExpandedId(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold">Webhook endpoints</h2>

      {isLoading ? (
        <Spinner />
      ) : (endpoints ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No webhook endpoints yet.</p>
      ) : (
        <div className="space-y-2">
          {(endpoints ?? []).map((ep: WebhookEndpointOut) => (
            <div key={ep.id} className="rounded-md border border-border">
              <div className="flex flex-wrap items-center justify-between gap-2 p-3">
                <div>
                  <p className="text-sm">{ep.url}</p>
                  <p className="text-xs text-muted-foreground">{ep.event_types.join(", ")}</p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge className={endpointStatusBadgeClass(ep.status)}>{ep.status}</Badge>
                  <span className="text-xs text-muted-foreground">
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
            </div>
          ))}
        </div>
      )}

      <form className="space-y-3" onSubmit={submitCreate}>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="webhook-url">
              Endpoint URL
            </label>
            <Input
              id="webhook-url"
              aria-label="Endpoint URL"
              placeholder="https://example.com/webhooks/csaas"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>
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

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {created && (
        <CopyOnceBox
          label="Webhook signing secret"
          value={created.secret}
          note="This secret is shown once and cannot be retrieved again. It signs every delivery to this endpoint (X-Webhook-Signature)."
          onDismiss={() => setCreated(null)}
        />
      )}
    </section>
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
  const { data, isLoading, error } = useAuditLog(api, filters, cursor);
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
    <section className="space-y-4">
      <h2 className="text-base font-semibold">Audit log</h2>

      <form className="flex flex-wrap items-end gap-2" onSubmit={applyFilters}>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="audit-action">
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
          <label className="block text-xs text-muted-foreground" htmlFor="audit-target-type">
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

      {isLoading && rows.length === 0 ? (
        <Spinner />
      ) : error ? (
        <p role="alert" className="text-sm text-destructive">
          {(error as Error).message}
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No audit entries yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Action</th>
                <th className="px-3 py-2 font-medium">Target</th>
                <th className="px-3 py-2 font-medium">Actor</th>
                <th className="px-3 py-2 font-medium">When</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="px-3 py-2 font-mono text-xs">{r.action}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {r.target_type}
                    {r.target_id ? ` (${r.target_id.slice(0, 8)})` : ""}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {r.actor_api_key_id
                      ? `API key ${r.actor_api_key_id.slice(0, 8)}`
                      : r.actor_user_id
                        ? `User ${r.actor_user_id.slice(0, 8)}`
                        : "—"}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
    </section>
  );
}

// =====================================================================================
// Usage + reconciliation (DR-2)
// =====================================================================================
function verdictBadgeClass(verdict: string): string {
  switch (verdict) {
    case "within_tolerance":
      return "bg-green-100 text-green-800";
    case "mismatch":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

function UsageSection() {
  const { api } = useAuth();
  const [date, setDate] = React.useState(todayUtc());
  const { data: usage, isLoading: usageLoading } = useUsage(api, date, date);
  const { data: reconciliation, isLoading: reconLoading } = useReconciliation(api, date);

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">Usage</h2>
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
      </div>

      {usageLoading || reconLoading ? (
        <Spinner />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Metric</th>
                <th className="px-3 py-2 font-medium">Ours</th>
                <th className="px-3 py-2 font-medium">Carrier</th>
                <th className="px-3 py-2 font-medium">Verdict</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(reconciliation?.items ?? []).map((item) => (
                <tr key={item.metric}>
                  <td className="px-3 py-2 font-mono text-xs">{item.metric}</td>
                  <td className="px-3 py-2">{item.ours}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {item.carrier ?? "—"}
                  </td>
                  <td className="px-3 py-2">
                    <Badge className={verdictBadgeClass(item.verdict)}>{item.verdict}</Badge>
                  </td>
                </tr>
              ))}
              {(reconciliation?.items ?? []).length === 0 && (usage ?? []).length === 0 && (
                <tr>
                  <td className="px-3 py-4 text-center text-xs text-muted-foreground" colSpan={4}>
                    No usage recorded for this date yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function PlatformPage() {
  return (
    <div className={cn("mx-auto max-w-4xl space-y-10 p-6")}>
      <h1 className="text-lg font-semibold">Platform</h1>
      <ApiKeysSection />
      <WebhooksSection />
      <AuditSection />
      <UsageSection />
    </div>
  );
}
