import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/primitives";
import { BANNER_PRIORITY, BannerSlot } from "@/components/shell/BannerSlot";
import { isOwner, useAuth } from "@/auth/AuthContext";
import {
  PREPAID_EMPTY_COPY,
  WARNING_COPY,
  useBillingSummary,
  type BalanceWarning,
} from "@/api/billing";

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
  const { api, me, orgId } = useAuth();
  const [dismissed, setDismissed] = useState<BalanceWarning | null>(readDismissedLevel);
  // Owner-only, deliberately: GET /api/v1/billing/summary is owner-only server-side, so this
  // gates on ownership instead of gate.can("settings:read") (which a non-owner admin also
  // holds) and fires NO request at all for anyone else - useBillingSummary's second argument
  // is react-query's `enabled`. A non-owner sees nothing from this component: the frontend
  // has no other readable source for the balance (/api/v1/me/capabilities, which every member
  // can read, carries no credit signal), a non-owner cannot top up anyway, and a failed
  // message already carries its own server-sourced failure_reason_public explanation at the
  // point of failure. While `me` is null this is false too - the correct fail-closed
  // direction: one extra beat before an owner's banner appears, never a 403 for a non-owner.
  const owner = isOwner(me, orgId);
  const summaryQ = useBillingSummary(api, owner);
  const navigate = useNavigate();

  if (summaryQ.isLoading || summaryQ.isError || summaryQ.data == null) return null;

  const warning = summaryQ.data.warning;
  if (!warning) return null;
  if (dismissed && WARNING_ORDER[warning] <= WARNING_ORDER[dismissed]) return null;

  const copy =
    warning === "empty" && summaryQ.data.telephony_prepaid
      ? PREPAID_EMPTY_COPY
      : WARNING_COPY[warning];

  return (
    <BannerSlot priority={BANNER_PRIORITY.credits}>
      {/* One slim line. The title and the body both stay - the body is the part that says
          whether sending has actually stopped - but they sit side by side and truncate
          rather than stacking into a card above the inbox. */}
      <div
        role="status"
        className="flex items-center gap-3 border-b border-border bg-muted px-4 py-1.5"
      >
        <p className="min-w-0 flex-1 truncate text-sm">
          <span className="font-semibold">{copy.title}</span>
          {/* The separator is its own aria-hidden node so the title and the body each stay
              a single exact-text element for assistive tech and for the tests. */}
          <span aria-hidden="true" className="text-muted-foreground"> — </span>
          <span className="text-muted-foreground">{copy.body}</span>
        </p>
        <div className="flex shrink-0 items-center gap-1">
          <Button type="button" size="sm" onClick={() => navigate("/settings/billing")}>
            Add credits
          </Button>
          <Button
            type="button"
            size="sm"
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
    </BannerSlot>
  );
}
