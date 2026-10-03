import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Button, MutationStatus, Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type {
  PlatformMessagingHealthOut,
  PlatformMessagingHealthRow,
} from "@/api/hooks";

// Messaging health across every workspace, for platform operators (Switchboard). Reads the
// signed-in operator's session; the shared ops token is no longer accepted (H4).
const MESSAGING_HEALTH_PATH = "/api/v1/platform/messaging/health?days=7";
const DELIVERY_DIGESTS_PATH = "/api/v1/platform/messaging/digests";
const OPS_DIGEST_TEST_PATH = "/api/v1/platform/messaging/ops-digest/test";

const DELIVERY_DIGESTS_QUERY_KEY = ["platform-delivery-digests"];

// An org that has never been configured keeps the backend defaults: 8am Central to whoever
// the backend resolves as owners & admins.
const DEFAULT_DIGEST_HOUR = 8;
const DEFAULT_DIGEST_TZ = "America/Chicago";

type DeliveryDigest = {
  org_id: string;
  org_name: string;
  enabled: boolean;
  hour: number;
  tz: string;
  recipients: string[];
  default_recipients: string[];
  last_sent_at: string | null;
};

type DeliveryDigestTestResult = { sent: boolean; day: string | null };
type OpsDigestTestResult = { sent: boolean };

const LEVEL_LABELS: Record<PlatformMessagingHealthRow["level"], string> = {
  ok: "Healthy",
  warn: "Warning",
  critical: "Critical",
  no_data: "No data",
};

const LEVEL_STYLES: Record<PlatformMessagingHealthRow["level"], string> = {
  ok: "bg-emerald-100 text-emerald-800",
  warn: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
  no_data: "bg-muted text-muted-foreground",
};

function formatRate(rate: number | null): string {
  return rate == null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

function formatShortDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function deliveryDigestPath(orgId: string): string {
  return `/api/v1/platform/orgs/${orgId}/delivery-digest`;
}

function deliveryDigestTestPath(orgId: string): string {
  return `/api/v1/platform/orgs/${orgId}/delivery-digest/test`;
}

function jsonInit(method: "PUT" | "POST", body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/**
 * One row's "Daily email" controls. Each row owns its own mutations so the pending state of
 * one workspace's toggle or test does not disable or overwrite another's.
 */
function DigestCell({
  row,
  digest,
}: {
  row: PlatformMessagingHealthRow;
  digest?: DeliveryDigest;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const toggleMutation = useMutation({
    mutationFn: (next: boolean) =>
      api.request(
        deliveryDigestPath(row.org_id),
        jsonInit("PUT", {
          enabled: next,
          hour: digest?.hour ?? DEFAULT_DIGEST_HOUR,
          tz: digest?.tz ?? DEFAULT_DIGEST_TZ,
          recipients: digest?.recipients ?? [],
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: DELIVERY_DIGESTS_QUERY_KEY });
    },
  });

  const testMutation = useMutation({
    mutationFn: () =>
      api.request<DeliveryDigestTestResult>(deliveryDigestTestPath(row.org_id), {
        method: "POST",
      }),
  });

  const recipients = digest?.recipients ?? [];
  const title =
    recipients.length > 0
      ? recipients.join(", ")
      : `Owners & admins: ${(digest?.default_recipients ?? []).join(", ")}`;

  return (
    <div className="flex items-center gap-2">
      <input
        type="checkbox"
        className="h-4 w-4 shrink-0"
        aria-label={`Daily email for ${row.org_name}`}
        title={title}
        checked={digest?.enabled ?? false}
        disabled={toggleMutation.isPending}
        onChange={(event) => toggleMutation.mutate(event.target.checked)}
      />
      <button
        type="button"
        aria-label={`Send test for ${row.org_name}`}
        className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
        onClick={() => testMutation.mutate()}
      >
        Test
      </button>
      <MutationStatus
        pending={testMutation.isPending}
        pendingLabel="Sending…"
        error={testMutation.error}
        success={
          testMutation.data
            ? testMutation.data.sent
              ? "Sent to you"
              : "No texts in 7 days"
            : null
        }
      />
      <MutationStatus error={toggleMutation.error} />
    </div>
  );
}

export function PlatformMessagingHealth() {
  const { api } = useAuth();

  const healthQuery = useQuery({
    queryKey: ["platform-messaging-health"],
    queryFn: () => api.request<PlatformMessagingHealthOut>(MESSAGING_HEALTH_PATH),
  });

  const digestsQuery = useQuery({
    queryKey: DELIVERY_DIGESTS_QUERY_KEY,
    queryFn: () => api.request<DeliveryDigest[]>(DELIVERY_DIGESTS_PATH),
  });

  const opsDigestMutation = useMutation({
    mutationFn: () =>
      api.request<OpsDigestTestResult>(OPS_DIGEST_TEST_PATH, { method: "POST" }),
  });

  const digestsByOrg = new Map<string, DeliveryDigest>();
  for (const digest of digestsQuery.data ?? []) {
    digestsByOrg.set(digest.org_id, digest);
  }

  return (
    <div className="rounded-md border border-border p-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Messaging health (all workspaces)</h2>
          <p className="text-sm text-muted-foreground">
            Delivery, spam-block and opt-out rates over the last 7 days, worst first.
          </p>
          <p className="text-sm text-muted-foreground">
            You get the all-workspace report every morning at 8:00 Central. Customers only get a
            daily email if you switch it on below; they always get warning emails when delivery
            drops.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={opsDigestMutation.isPending}
            onClick={() => opsDigestMutation.mutate()}
          >
            Email me the ops report
          </Button>
          <MutationStatus
            pending={opsDigestMutation.isPending}
            pendingLabel="Sending…"
            error={opsDigestMutation.error}
            success={
              opsDigestMutation.data
                ? opsDigestMutation.data.sent
                  ? "Sent. Check your inbox."
                  : "Nothing sent."
                : null
            }
          />
        </div>
      </div>

      {healthQuery.isLoading ? (
        <Spinner label="Loading messaging health" />
      ) : healthQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(healthQuery.error)}
        </p>
      ) : !healthQuery.data ? null : healthQuery.data.rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No workspaces with text traffic yet.</p>
      ) : (
        // Rows arrive worst-first from the API; the table never re-sorts them.
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              <th className="py-2 pr-4 font-medium">Workspace</th>
              <th className="py-2 pr-4 font-medium">Level</th>
              <th className="py-2 pr-4 font-medium">Volume</th>
              <th className="py-2 pr-4 font-medium">Delivery</th>
              <th className="py-2 pr-4 font-medium">Spam blocks</th>
              <th className="py-2 pr-4 font-medium">Opt-outs</th>
              <th className="py-2 font-medium">Breached since</th>
              <th className="py-2 pl-4 font-medium">Daily email</th>
            </tr>
          </thead>
          <tbody>
            {healthQuery.data.rows.map((row) => (
              <tr key={row.org_id} className="border-b border-border last:border-0">
                <td className="py-2 pr-4">{row.org_name}</td>
                <td className="py-2 pr-4">
                  <span
                    className={cn(
                      "rounded-full px-2 py-0.5 text-xs font-medium",
                      LEVEL_STYLES[row.level],
                    )}
                  >
                    {LEVEL_LABELS[row.level]}
                  </span>
                </td>
                <td className="py-2 pr-4">{row.volume}</td>
                <td className="py-2 pr-4">{formatRate(row.delivery_rate)}</td>
                <td className="py-2 pr-4">{formatRate(row.spam_block_rate)}</td>
                <td className="py-2 pr-4">{formatRate(row.opt_out_rate)}</td>
                <td className="py-2">{formatShortDate(row.first_breached_at)}</td>
                <td className="py-2 pl-4">
                  {digestsQuery.isError ? (
                    "—"
                  ) : (
                    <DigestCell row={row} digest={digestsByOrg.get(row.org_id)} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
