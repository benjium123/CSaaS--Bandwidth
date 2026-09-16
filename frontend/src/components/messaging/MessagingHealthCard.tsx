import { useAuth } from "@/auth/AuthContext";
import { useMessagingHealth, type MessagingHealthOut } from "@/api/hooks";
import { Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const LEVEL_LABELS: Record<MessagingHealthOut["level"], string> = {
  ok: "Healthy",
  warn: "Warning",
  critical: "Critical",
  no_data: "No data",
};

const LEVEL_STYLES: Record<MessagingHealthOut["level"], string> = {
  ok: "bg-emerald-100 text-emerald-800",
  warn: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
  no_data: "bg-muted text-muted-foreground",
};

function formatRate(rate: number | null): string {
  return rate == null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

export function MessagingHealthCard({ days }: { days: number }) {
  const { api } = useAuth();
  const { data, isLoading, error } = useMessagingHealth(api, days);

  if (isLoading) {
    return (
      <div className="rounded-md border border-border p-4">
        <h2 className="mb-3 text-sm font-medium">Messaging health</h2>
        <Spinner label="Loading messaging health" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-md border border-border p-4">
        <h2 className="mb-3 text-sm font-medium">Messaging health</h2>
        <p role="alert" className="text-sm text-destructive">
          {(error as Error).message}
        </p>
      </div>
    );
  }

  if (!data) return null;

  // A window with no terminal outbound traffic has no rate to show - rendering 0% here
  // would read as "nothing is being delivered" rather than "nothing was sent".
  if (data.level === "no_data") {
    return (
      <div className="rounded-md border border-border p-4">
        <h2 className="mb-3 text-sm font-medium">Messaging health</h2>
        <p className="text-sm text-muted-foreground">No text traffic in this range yet.</p>
      </div>
    );
  }

  const failed = data.failed_by_class;
  const hasFailures =
    failed.spam_blocked > 0 ||
    failed.carrier_rejected > 0 ||
    failed.invalid_destination > 0 ||
    failed.opted_out > 0 ||
    failed.unknown > 0;

  return (
    <div className="rounded-md border border-border p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium">Messaging health</h2>
        <span
          className={cn("rounded-full px-2 py-0.5 text-xs font-medium", LEVEL_STYLES[data.level])}
        >
          {LEVEL_LABELS[data.level]}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div>
          <p className="text-sm font-medium">{formatRate(data.delivery_rate)}</p>
          <p className="text-xs text-muted-foreground">Delivery</p>
          <p className="text-xs text-muted-foreground">of {data.volume} texts</p>
        </div>
        <div>
          <p className="text-sm font-medium">{formatRate(data.spam_block_rate)}</p>
          <p className="text-xs text-muted-foreground">Spam blocks</p>
          <p className="text-xs text-muted-foreground">of {data.volume} texts</p>
        </div>
        <div>
          <p className="text-sm font-medium">{formatRate(data.opt_out_rate)}</p>
          <p className="text-xs text-muted-foreground">Opt-outs</p>
          <p className="text-xs text-muted-foreground">of {data.volume} texts</p>
        </div>
      </div>

      {data.reasons.length > 0 && (
        <div className="mt-4 space-y-1">
          {data.reasons.map((reason) => (
            <p key={reason} className="text-sm text-foreground">
              {reason}
            </p>
          ))}
        </div>
      )}

      {hasFailures && (
        <p className="mt-4 text-xs text-muted-foreground">
          Failures: {failed.spam_blocked} spam blocked · {failed.carrier_rejected} rejected ·{" "}
          {failed.invalid_destination} invalid number · {failed.opted_out} opted out ·{" "}
          {failed.unknown} unknown
        </p>
      )}
    </div>
  );
}
