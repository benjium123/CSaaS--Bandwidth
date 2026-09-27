/**
 * "View as workspace" - starts an H3 support view of a customer workspace.
 *
 * The button is only the trigger: the API client itself asks the operator for a reason
 * when the server demands one (ops_reason_required) and retries, so there is no reason
 * field here. On success the grant is parked in sessionStorage and the tab moves to the
 * workspace inbox, where ViewAsBanner keeps a countdown and offers the way back out.
 */
import * as React from "react";

import { useAuth } from "@/auth/AuthContext";
import { saveViewAs } from "@/auth/viewAs";
import { Button, mutationErrorMessage } from "@/components/ui/primitives";

type StartViewResponse = {
  id: string;
  org_id: string;
  org_name: string;
  expires_at: string;
  permissions: string[];
};

export function ViewAsButton({ orgId, orgName }: { orgId: string; orgName: string }) {
  const { api, me } = useAuth();
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);

  async function startView() {
    setPending(true);
    setError(null);
    try {
      const result = await api.request<StartViewResponse>("/api/v1/ops/view-as", {
        method: "POST",
        json: { org_id: orgId },
      });
      saveViewAs({
        id: result.id,
        orgId: result.org_id,
        // Prefer the server's canonical name; fall back to the one we were handed.
        orgName: result.org_name || orgName,
        expiresAt: result.expires_at,
        permissions: result.permissions,
        prevOrgId: api.auth.orgId,
      });
      window.location.assign("/inbox");
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  }

  if (!me?.operator_permissions?.includes("ops:support")) return null;

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button type="button" variant="outline" onClick={startView} disabled={pending}>
        View as workspace
      </Button>
      <span className="text-xs text-muted-foreground">
        Read-only, 30 minutes, the owner is told.
      </span>
      {error != null ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(error)}
        </p>
      ) : null}
    </div>
  );
}
