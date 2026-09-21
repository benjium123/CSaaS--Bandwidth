import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { Spinner } from "@/components/ui/primitives";

import { BrandsPanel } from "./BrandsPanel";
import { CampaignsPanel } from "./CampaignsPanel";

/**
 * The workspace's 10DLC registration screen.
 *
 * Registration is per workspace, per EIN: a workspace registers its own brand and its own
 * campaigns, and shares none of it with any other workspace.
 *
 * Individual accounts cannot register for 10DLC at all, so the inner panels - each of
 * which fires a query on mount - must never be mounted for them. The account-type check
 * lives in a wrapper so that the hooks below are still called unconditionally.
 */
export function TenDlcRegistration(): JSX.Element {
  const { me, orgId, ready } = useAuth();

  // "We have not asked yet" is not "business". Until /auth/me has landed we cannot know
  // the account type, so show loading rather than mounting the panels.
  if (!ready || me == null) {
    return <Spinner label="Loading registration" />;
  }

  const membership = me.memberships.find((m) => m.org_id === orgId);
  // An absent account_type is a legacy business membership (see Membership in
  // AuthContext): only an explicit "individual" is treated as individual.
  if (membership?.account_type === "individual") {
    return <IndividualRegistrationNotice />;
  }

  return <TenDlcRegistrationInner />;
}

/**
 * The individual-account notice. It is a separate component so that the gate above can
 * return before any of the inner panels' hooks run.
 */
function IndividualRegistrationNotice(): JSX.Element {
  return (
    <SurfaceCard>
      <div className="flex flex-col gap-[18px]">
        <div className="flex flex-col gap-[6px]">
          <SectionLabel>10DLC registration</SectionLabel>
          <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
            Individual accounts support calling only. SMS and MMS are unavailable.
          </p>
        </div>
      </div>
    </SurfaceCard>
  );
}

/**
 * The business 10DLC screen. Split out so that the account-type gate in
 * `TenDlcRegistration` can return before this component - and therefore before
 * BrandsPanel and CampaignsPanel - is ever mounted.
 */
function TenDlcRegistrationInner(): JSX.Element {
  const gate = useGate();

  if (gate.isLoading) {
    return <Spinner label="Loading registration" />;
  }

  // The early return is load-bearing: BrandsPanel and CampaignsPanel each fire a query on
  // mount, and a user without `compliance:read` must never make those requests.
  if (!gate.can("compliance:read")) {
    return (
      <ConsoleEmpty>
        You do not have access to 10DLC registration for this workspace.
      </ConsoleEmpty>
    );
  }

  return (
    <SurfaceCard>
      <div className="flex flex-col gap-[18px]">
        <div className="flex flex-col gap-[6px]">
          <SectionLabel>10DLC registration</SectionLabel>
          <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
            10DLC is registered per company, per EIN. This workspace registers its own brand
            and campaigns.
          </p>
        </div>
        <BrandsPanel />
        <CampaignsPanel />
      </div>
    </SurfaceCard>
  );
}
