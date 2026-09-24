import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { Spinner } from "@/components/ui/primitives";

import { BrandsPanel } from "./BrandsPanel";
import { CampaignsPanel } from "./CampaignsPanel";
import { TextingRegistrationCard } from "./TextingRegistrationCard";

/**
 * The workspace's 10DLC registration screen.
 *
 * Registration is per workspace, per EIN: a workspace registers its own brand and its own
 * campaigns, and shares none of it with any other workspace.
 *
 * Available to every account with compliance permissions. Company details are
 * collected here, separately from personal identity verification.
 */
export function TenDlcRegistration(): JSX.Element {
  const { me, ready } = useAuth();

  // Wait for authentication before mounting registration queries.
  if (!ready || me == null) {
    return <Spinner label="Loading registration" />;
  }

  return <TenDlcRegistrationInner />;
}

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
        <TextingRegistrationCard />
        <BrandsPanel />
        <CampaignsPanel />
      </div>
    </SurfaceCard>
  );
}
