import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/primitives";
import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { WARNING_COPY, useBillingSummary, type BalanceWarning } from "@/api/billing";

const STORAGE_KEY = "csaas.billing.banner.dismissed";

const WARNING_ORDER: Record<BalanceWarning, number> = {
  low: 0,
  critical: 1,
  empty: 2,
};

function readDismissedLevel(): BalanceWarning | null {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    return stored === "low" || stored === "critical" || stored === "empty" ? stored : null;
  } catch {
    return null;
  }
}

function writeDismissedLevel(level: BalanceWarning): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, level);
  } catch {
    // Private mode / storage disabled - the dismissal simply won't persist.
  }
}

export function LowBalanceBanner() {
  const { api } = useAuth();
  const gate = useGate();
  const [dismissed, setDismissed] = useState<BalanceWarning | null>(readDismissedLevel);
  const summaryQ = useBillingSummary(api, gate.can("settings:read"));
  const navigate = useNavigate();

  if (summaryQ.isLoading || summaryQ.isError || summaryQ.data == null) return null;

  const warning = summaryQ.data.warning;
  if (!warning) return null;
  if (dismissed && WARNING_ORDER[warning] <= WARNING_ORDER[dismissed]) return null;

  const copy = WARNING_COPY[warning];

  return (
    <div role="status" className="border-b border-border bg-muted px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-semibold">{copy.title}</p>
          <p className="mt-0.5 text-sm text-muted-foreground">{copy.body}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button type="button" onClick={() => navigate("/settings/billing")}>
            Add credits
          </Button>
          <Button
            type="button"
            variant="ghost"
            aria-label="Dismiss"
            onClick={() => {
              setDismissed(warning);
              writeDismissedLevel(warning);
            }}
          >
            Dismiss
          </Button>
        </div>
      </div>
    </div>
  );
}
