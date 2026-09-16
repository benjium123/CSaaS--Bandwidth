import { useNavigate } from "react-router-dom";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { statusCopy, useKycProfile } from "@/api/kyc";
import { Button } from "@/components/ui/primitives";

/** P41: tells the workspace why calling and texting are (or soon will be) unavailable.
 * Not dismissible - it reflects a hard gate, not a nudge. Hidden once approved. */
export function VerificationBanner() {
  const { api, me, orgId } = useAuth();
  const navigate = useNavigate();
  const profileQ = useKycProfile(api, Boolean(orgId) && hasPermission(me, orgId, "org:read"));

  if (!profileQ.data) return null;
  const copy = statusCopy(profileQ.data.status);
  if (!copy) return null;
  const actionable = !["submitted", "in_review", "rejected", "suspended"].includes(
    profileQ.data.status,
  );

  return (
    <div
      role="status"
      aria-label="Business verification"
      className="border-b border-border bg-muted px-4 py-3"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-semibold">{copy.title}</p>
          <p className="mt-0.5 text-sm text-muted-foreground">{copy.body}</p>
        </div>
        {actionable && (
          <Button type="button" onClick={() => navigate("/settings/verification")}>
            {profileQ.data.status === "draft" ? "Get verified" : "Open verification"}
          </Button>
        )}
      </div>
    </div>
  );
}
