import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type {
  PlatformMessagingHealthOut,
  PlatformMessagingHealthRow,
} from "@/api/hooks";

// Messaging health across every workspace, for platform operators (Switchboard). Reads the
// signed-in operator's session; the shared ops token is no longer accepted (H4).
const MESSAGING_HEALTH_PATH = "/api/v1/platform/messaging/health?days=7";

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

export function PlatformMessagingHealth() {
  const { api } = useAuth();

  const healthQuery = useQuery({
    queryKey: ["platform-messaging-health"],
    queryFn: () => api.request<PlatformMessagingHealthOut>(MESSAGING_HEALTH_PATH),
  });

  return (
    <div className="rounded-md border border-border p-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Messaging health (all workspaces)</h2>
          <p className="text-sm text-muted-foreground">
            Delivery, spam-block and opt-out rates over the last 7 days, worst first.
          </p>
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
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
