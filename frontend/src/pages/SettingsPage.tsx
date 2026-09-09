import * as React from "react";
import { Navigate, NavLink, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import { fetchProviderAccounts } from "@/api/providers";
import { SpendCard } from "@/components/spend/SpendCard";
import {
  Button,
  Card,
  EmptyState,
  panelId,
  Pill,
  Section,
  Spinner,
  tabId,
  TabPanel,
  Tabs,
} from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { TeamPage } from "@/pages/TeamPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { InboxSettingsPage } from "@/pages/InboxSettingsPage";
import { NumbersPage } from "@/pages/NumbersPage";
import { ProvidersPage } from "@/pages/ProvidersPage";
import { FlowsPage } from "@/pages/FlowsPage";
import { QueuesPage } from "@/pages/QueuesPage";
import { AgentPage } from "@/pages/AgentPage";
import { AppointmentsPage } from "@/pages/AppointmentsPage";
import { PlatformPage } from "@/pages/PlatformPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { SETTINGS_SECTIONS, type SettingsSectionId } from "./settingsSections";

export type { SettingsSectionId } from "./settingsSections";
export { SETTINGS_SECTIONS } from "./settingsSections";

function registrationLabel(state: string): string {
  if (state === "none") return "Not started";
  if (state === "pending") return "In review";
  if (state === "approved") return "Approved";
  return state;
}

function useSettingsTab<T extends { id: string; label: string }>(
  tabs: T[],
  defaultId: string,
) {
  const [searchParams, setSearchParams] = useSearchParams();
  const param = searchParams.get("tab");
  const value = param !== null && tabs.some((tab) => tab.id === param) ? param : defaultId;

  const onChange = React.useCallback(
    (id: string) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("tab", id);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  return { value, onChange };
}

function SettingsTabs({ id, tabs, value, onChange, ariaLabel, children }: {
  id: string;
  tabs: { id: string; label: string }[];
  value: string;
  onChange: (next: string) => void;
  ariaLabel: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <Tabs
        tabs={tabs}
        value={value}
        onChange={onChange}
        ariaLabel={ariaLabel}
        id={id}
      />
      {tabs.map((tab) =>
        tab.id === value ? (
          <TabPanel key={tab.id} tabsId={id} id={tab.id}>
            {children}
          </TabPanel>
        ) : (
          <div
            key={tab.id}
            role="tabpanel"
            hidden
            id={panelId(id, tab.id)}
            aria-labelledby={tabId(id, tab.id)}
          />
        ),
      )}
    </div>
  );
}

function TeamSettingsSection() {
  const tabs = [
    { id: "members", label: "Members & roles" },
    { id: "security", label: "Security" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "members");

  return (
    <SettingsTabs
      id="settings-team"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="Team settings"
    >
      {value === "members" ? <TeamPage /> : <SettingsSecurityPage />}
    </SettingsTabs>
  );
}

function CallingSettingsSection() {
  const tabs = [
    { id: "flows", label: "Call flows" },
    { id: "queues", label: "Queues" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "flows");

  return (
    <SettingsTabs
      id="settings-calling"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="Calling settings"
    >
      {value === "flows" ? <FlowsPage /> : <QueuesPage />}
    </SettingsTabs>
  );
}

function AiSettingsSection() {
  const tabs = [
    { id: "agent", label: "Agent" },
    { id: "appointments", label: "Appointments" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "agent");

  return (
    <SettingsTabs
      id="settings-ai"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="AI settings"
    >
      {value === "agent" ? <AgentPage /> : <AppointmentsPage />}
    </SettingsTabs>
  );
}

function WorkspaceSection() {
  const { api } = useAuth();
  const gate = useGate();
  const currentOrgQuery = useQuery({
    queryKey: ["org", "profile"],
    queryFn: () => api.request<{ id: string; name: string; slug: string }>("/api/v1/orgs/current"),
    retry: false,
  });

  return (
    <div className="space-y-3">
      <Section title="Workspace">
        {currentOrgQuery.isLoading ? (
          <Spinner label="Loading workspace" />
        ) : currentOrgQuery.error ? (
          <div className="space-y-2">
            <p role="alert">Failed to load workspace.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => currentOrgQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : (
          <Card>
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">Name</dt>
                <dd>{currentOrgQuery.data?.name}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">Short name</dt>
                <dd>{currentOrgQuery.data?.slug}</dd>
              </div>
            </dl>
          </Card>
        )}
      </Section>

      {gate.org ? (
        <Card>
          <div className="flex flex-wrap gap-2">
            <Pill tone={gate.org.has_provider ? "success" : "neutral"}>
              {gate.org.has_provider ? "Provider connected" : "No provider yet"}
            </Pill>
            <Pill tone={gate.org.has_number ? "success" : "neutral"}>
              {gate.org.has_number ? "Number added" : "No number yet"}
            </Pill>
            <Pill tone="info">{gate.org.member_count} {gate.org.member_count === 1 ? "member" : "members"}</Pill>
            <Pill tone="info">{registrationLabel(gate.org.registration_state)}</Pill>
          </div>
        </Card>
      ) : null}
    </div>
  );
}

function MessagingSection() {
  const navigate = useNavigate();

  return (
    <EmptyState
      title="Messaging settings are on the way"
      description="Texting registration lives with your phone numbers for now."
      action={
        <Button type="button" variant="outline" onClick={() => navigate("/settings/numbers")}>
          Go to phone numbers
        </Button>
      }
    />
  );
}

function BillingSettingsSection() {
  const tabs = [
    { id: "usage", label: "Usage" },
    { id: "dashboard", label: "Dashboard" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "usage");

  return (
    <SettingsTabs
      id="settings-billing"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="Billing settings"
    >
      {/* Dashboard is mounted here (rather than routed directly) until P30 Reports
          replaces it; /dashboard redirects to this tab so the page stays reachable. */}
      {value === "usage" ? <BillingUsageSection /> : <DashboardPage />}
    </SettingsTabs>
  );
}

function BillingUsageSection() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const accountsQuery = useQuery({
    queryKey: ["provider-accounts"],
    queryFn: () => fetchProviderAccounts(api),
    retry: false,
  });

  return (
    <Section title="Billing & usage">
      {accountsQuery.isLoading ? (
        <Spinner label="Loading billing" />
      ) : accountsQuery.error ? (
        <div className="space-y-2">
          <p role="alert">Failed to load billing.</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => accountsQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : accountsQuery.data && accountsQuery.data.length === 0 ? (
        <EmptyState
          title="No spend yet"
          description="Connect a provider to see what you are spending."
          action={
            <Button type="button" variant="outline" onClick={() => navigate("/settings/providers")}>
              Connect a provider
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3">
          {accountsQuery.data?.map((account) => (
            <SpendCard key={account.id} provider={account.provider} />
          ))}
        </div>
      )}
    </Section>
  );
}

function SectionContent({ id }: { id: SettingsSectionId }) {
  switch (id) {
    case "workspace":
      return <WorkspaceSection />;
    case "team":
      return <TeamSettingsSection />;
    case "inboxes":
      return <InboxSettingsPage />;
    case "numbers":
      return <NumbersPage />;
    case "providers":
      return <ProvidersPage />;
    case "messaging":
      return <MessagingSection />;
    case "calling":
      return <CallingSettingsSection />;
    case "ai":
      return <AiSettingsSection />;
    case "billing":
      return <BillingSettingsSection />;
    case "developers":
      return <PlatformPage />;
  }

  return null;
}

export function SettingsPage() {
  const { section } = useParams<{ section: string }>();
  const gate = useGate();
  const current = SETTINGS_SECTIONS.find((s) => s.id === section);

  if (!section || !current) {
    return <Navigate to="/settings/workspace" replace />;
  }

  const canView = !gate.isLoading && gate.can(current.permission);

  return (
    <div className="dark flex h-full flex-col overflow-hidden sm:flex-row">
      <nav
        aria-label="Settings"
        className="w-full shrink-0 overflow-x-auto border-b border-border p-2 sm:w-56 sm:overflow-y-auto sm:border-b-0 sm:border-r"
      >
        {gate.isLoading ? (
          <Spinner label="Loading settings" />
        ) : (
          <div className="flex flex-row gap-1 sm:flex-col">
            {SETTINGS_SECTIONS.filter((s) => gate.can(s.permission)).map((s) => (
              <NavLink
                key={s.id}
                to={`/settings/${s.id}`}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-2 text-sm",
                    isActive
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:bg-muted",
                  )
                }
              >
                {s.label}
              </NavLink>
            ))}
          </div>
        )}
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="p-4">
          {gate.isLoading ? (
            <Spinner label="Loading settings" />
          ) : !canView ? (
            <Card>
              <p className="text-sm font-medium">You do not have access to this setting.</p>
            </Card>
          ) : (
            <SectionContent id={current.id} />
          )}
        </div>
      </div>
    </div>
  );
}
