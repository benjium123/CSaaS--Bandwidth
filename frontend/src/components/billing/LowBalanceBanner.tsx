import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card } from "@/components/ui/primitives";
import { BANNER_PRIORITY, BannerSlot } from "@/components/shell/BannerSlot";
import { isOwner, useAuth } from "@/auth/AuthContext";
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

  const warningOrNull = summaryQ.data.warning;
  if (!warningOrNull) return null;
  // Reassigned to a binding TS can prove non-null wherever it is captured below, including
  // inside `dismiss` - narrowing a `const` from an early return does not survive into a
  // nested function's closure.
  const warning: BalanceWarning = warningOrNull;
  if (dismissed && WARNING_ORDER[warning] <= WARNING_ORDER[dismissed]) return null;

  function dismiss() {
    setDismissed(warning);
    writeDismissedLevel(warning);
  }

  // Empty is worse than low/critical when the workspace's OWN texting and calling (not just
  // the AI assistant) are prepaid off this balance: sending and receiving are both actually
  // stopped, not just degraded, so this gets a modal that has to be dismissed on purpose
  // rather than a slim banner that scrolls out of view with the inbox.
  if (warning === "empty" && summaryQ.data.telephony_prepaid) {
    return (
      <div
        role="presentation"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      >
        <Card role="dialog" aria-modal="true" aria-labelledby="low-balance-modal-title" className="w-full max-w-md">
          <h2 id="low-balance-modal-title" className="text-base font-semibold">
            Your balance is empty
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Outgoing texts and calls are paused and incoming calls are being declined until you
            add credit or buy a bundle.
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <Button type="button" onClick={() => navigate("/settings/billing")}>
              Add credit
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => navigate("/settings/billing#bundles")}
            >
              Buy bundles
            </Button>
            <Button type="button" variant="ghost" onClick={dismiss}>
              Not now
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  const copy = WARNING_COPY[warning];

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
            onClick={dismiss}
          >
            Dismiss
          </Button>
        </div>
      </div>
    </BannerSlot>
  );
}
