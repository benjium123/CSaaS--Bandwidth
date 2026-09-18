import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { Button, Textarea, mutationErrorMessage } from "@/components/ui/primitives";

/** P43: shown when the safety monitor paused or restricted calling and texting. The business
 * can explain what happened; an operator reviews it. Hidden while everything is normal. */

type Status = { level: "normal" | "restricted" | "paused"; message: string | null; appealed_at: string | null };

export function MonitoringBanner() {
  const { api, me, orgId } = useAuth();
  const qc = useQueryClient();
  // `Boolean(orgId) &&` used to be needed here because hasPermission returned true for a
  // null org - this is one of the two sites that noticed and patched it by hand, because
  // the answer gated a FETCH rather than a render. hasPermission now denies what it does
  // not know, so the guard is redundant and is removed rather than left to read as
  // superstition to the next person.
  const canRead = hasPermission(me, orgId, "org:read");
  const canAppeal = hasPermission(me, orgId, "org:update");
  const [open, setOpen] = React.useState(false);
  const [text, setText] = React.useState("");
  const status = useQuery({
    queryKey: ["monitoring", "status", orgId],
    queryFn: () => api.request<Status>("/api/v1/monitoring/status"),
    enabled: canRead,
    staleTime: 60_000,
    refetchInterval: 120_000,
  });
  const appeal = useMutation({
    mutationFn: () => api.request("/api/v1/monitoring/appeal", { method: "POST", json: { explanation: text } }),
    onSuccess: () => {
      setOpen(false);
      void qc.invalidateQueries({ queryKey: ["monitoring", "status", orgId] });
    },
  });

  const data = status.data;
  if (!data || data.level === "normal" || !data.message) return null;
  const paused = data.level === "paused";

  return (
    <div
      role="status"
      aria-label="Account review"
      className={`border-b px-4 py-3 ${paused ? "border-red-500/40 bg-red-500/10" : "border-amber-500/40 bg-amber-500/10"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold">
            {paused ? "Calling and texting are paused" : "Calling and texting are limited for now"}
          </p>
          <p className="mt-0.5 text-sm text-muted-foreground">{data.message}</p>
          {data.appealed_at && (
            <p className="mt-1 text-xs text-muted-foreground">
              We received your explanation on {new Date(data.appealed_at).toLocaleString()}. Our team will get back to you.
            </p>
          )}
        </div>
        {canAppeal && !data.appealed_at && !open && (
          <Button type="button" variant="outline" onClick={() => setOpen(true)}>
            Tell us what happened
          </Button>
        )}
      </div>
      {open && (
        <div className="mt-3 space-y-2">
          <Textarea
            aria-label="Explanation for the review team"
            rows={3}
            placeholder="What were you sending or calling about? Anything that helps us check quickly."
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="flex gap-2">
            <Button type="button" disabled={text.trim().length < 20 || appeal.isPending} onClick={() => appeal.mutate()}>
              Send to the review team
            </Button>
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
          {appeal.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(appeal.error)}</p>}
        </div>
      )}
    </div>
  );
}
