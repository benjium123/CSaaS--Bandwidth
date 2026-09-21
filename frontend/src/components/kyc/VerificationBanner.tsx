import { useNavigate } from "react-router-dom";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { statusCopy, useKycProfile } from "@/api/kyc";
import { Button } from "@/components/ui/primitives";
import { BANNER_PRIORITY, BannerSlot } from "@/components/shell/BannerSlot";

/** P41: tells the workspace why calling and texting are (or soon will be) unavailable.
 * Not dismissible - it reflects a hard gate, not a nudge. Hidden once approved. */
export function VerificationBanner() {
  const { api, me, orgId } = useAuth();
  const navigate = useNavigate();
  // The `Boolean(orgId) &&` that used to be here is gone: hasPermission no longer returns
  // true for an org it has not been given. See the note in MonitoringBanner.
  const profileQ = useKycProfile(api, hasPermission(me, orgId, "org:read"));

  if (!profileQ.data) return null;
  const accountType = profileQ.data.account_type ?? "business";
  const isIndividual = accountType === "individual";
  const copy = statusCopy(profileQ.data.status, accountType);
  if (!copy) return null;
  const actionable = !["submitted", "in_review", "rejected", "suspended"].includes(
    profileQ.data.status,
  );

  return (
    <BannerSlot priority={BANNER_PRIORITY.verification}>
      <div
        role="status"
        aria-label={isIndividual ? "Identity verification" : "Business verification"}
        className="flex items-center gap-[11px] border-b border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-4 py-[9px]"
      >
        <p className="min-w-0 flex-1 truncate text-[13px] text-[hsl(var(--cx-text))]">
          <span className="font-semibold">{copy.title}</span>
          <span aria-hidden="true" className="text-[hsl(var(--cx-muted))]"> — </span>
          <span className="text-[hsl(var(--cx-subtle))]">{copy.body}</span>
        </p>
        {actionable && (
          <Button
            type="button"
            size="sm"
            className="shrink-0"
            onClick={() => navigate("/onboarding")}
          >
            {profileQ.data.status === "draft"
              ? isIndividual
                ? "Verify your identity"
                : "Get verified"
              : "Open verification"}
          </Button>
        )}
      </div>
    </BannerSlot>
  );
}
