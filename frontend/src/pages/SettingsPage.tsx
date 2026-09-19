import * as React from "react";
import { Navigate, NavLink, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { hasPermission, isOwner, useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import { DataRetentionCard } from "@/components/settings/DataRetentionCard";
import { CreditsSection } from "@/components/billing/CreditsSection";
import { fetchProviderAccounts } from "@/api/providers";
import { SpendCard } from "@/components/spend/SpendCard";
import { AiProvidersTab } from "@/components/assistants/AiProvidersTab";
import { KnowledgeTab } from "@/components/assistants/KnowledgeTab";
import {
  Button,
  EmptyState,
  panelId,
  Pill,
  Section,
  Spinner,
  tabId,
  TabPanel,
  Tabs,
} from "@/components/ui/primitives";
import { SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { cn } from "@/lib/utils";
import { TeamPage } from "@/pages/TeamPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { InboxSettingsPage } from "@/pages/InboxSettingsPage";
import { NumbersPage } from "@/pages/NumbersPage";
import { ProvidersPage } from "@/pages/ProvidersPage";
import { FlowsPage } from "@/pages/FlowsPage";
import { QueuesPage } from "@/pages/QueuesPage";
import { SettingsCallingPage } from "@/pages/SettingsCallingPage";
import { AgentPage } from "@/pages/AgentPage";
import { AppointmentsPage } from "@/pages/AppointmentsPage";
import { PlatformPage } from "@/pages/PlatformPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { VerifyBusinessPage } from "@/pages/VerifyBusinessPage";
import {
  canViewSettingsSection,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from "./settingsSections";
import { INBOX_RAIL_PATHS, useRailNav } from "@/components/shell/Sidebar";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { TenDlcRegistration } from "@/components/registration";
import { TollFreeVerificationCard } from "@/components/registration/TollFreeVerificationCard";

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
    <div className="space-y-[14px]">
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
  // P29: what every call does (recording, the announcement, the call results list) comes
  // first; the per-number routing editors stay one tab over.
  const tabs = [
    { id: "general", label: "Recording & results" },
    { id: "flows", label: "Call flows" },
    { id: "queues", label: "Queues" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "general");

  return (
    <SettingsTabs
      id="settings-calling"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="Calling settings"
    >
      {value === "general" ? (
        <SettingsCallingPage />
      ) : value === "flows" ? (
        <FlowsPage />
      ) : (
        <QueuesPage />
      )}
    </SettingsTabs>
  );
}

function AiSettingsSection() {
  const gate = useGate();
  // The section itself is already gated on settings:read (see SETTINGS_SECTIONS); a
  // member who can only read must still see every control here, just unable to use it.
  // fail-closed: useGate's `can` returns false while loading, same as everywhere else.
  const canWrite = gate.can("settings:write");

  const tabs = [
    { id: "assistants", label: "Assistants" },
    { id: "providers", label: "Providers" },
    { id: "knowledge", label: "Knowledge" },
    { id: "appointments", label: "Appointments" },
  ];
  // P23a: the old default was "agent", which no longer exists as a tab id. An old
  // bookmark carrying ?tab=agent falls through useSettingsTab's membership check and
  // lands on Assistants - the same page it used to open, under its plain name.
  const { value, onChange } = useSettingsTab(tabs, "assistants");

  return (
    <SettingsTabs
      id="settings-ai"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="AI settings"
    >
      {/* A native <fieldset disabled> cascades to every descendant form control (button,
          input, select) regardless of nesting depth, so read-only access here does not
          require threading a prop through each of the four tabs individually. */}
      <fieldset disabled={!canWrite} className="m-0 min-w-0 border-0 p-0">
        {!canWrite && (
          <p className="mb-3 text-xs text-muted-foreground">
            You can view this, but only an admin can make changes here.
          </p>
        )}
        {value === "assistants" ? (
          <AgentPage />
        ) : value === "providers" ? (
          <AiProvidersTab />
        ) : value === "knowledge" ? (
          <KnowledgeTab />
        ) : (
          <AppointmentsPage />
        )}
      </fieldset>
    </SettingsTabs>
  );
}

/** P27: Workspace gained a second thing to say, so it gained tabs. "General" is what the
 * workspace IS; "Data" is how long it keeps what it collects. */
function WorkspaceSection() {
  const { me, orgId } = useAuth();
  const canReadRetention = hasPermission(me, orgId, "settings:read");

  const tabs = canReadRetention
    ? [
        { id: "general", label: "General" },
        { id: "data", label: "Data" },
      ]
    : [{ id: "general", label: "General" }];
  const { value, onChange } = useSettingsTab(tabs, "general");

  return (
    <SettingsTabs
      id="settings-workspace"
      tabs={tabs}
      value={value}
      onChange={onChange}
      ariaLabel="Workspace settings"
    >
      {value === "data" ? <DataRetentionCard /> : <WorkspaceGeneral />}
    </SettingsTabs>
  );
}

function WorkspaceGeneral() {
  const { api } = useAuth();
  const gate = useGate();
  const currentOrgQuery = useQuery({
    queryKey: ["org", "profile"],
    queryFn: () => api.request<{ id: string; name: string; slug: string }>("/api/v1/orgs/current"),
    retry: false,
  });

  return (
    <div className="space-y-[14px]">
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
          <SurfaceCard>
            {/* The reference's panel fields: muted key, value on the right, a hairline
                between rows rather than a gap. */}
            <dl className="text-[13.5px] [&>div+div]:border-t [&>div+div]:border-[hsl(var(--cx-line))]">
              <div className="flex justify-between gap-4 py-[11px]">
                <dt className="text-[hsl(var(--cx-muted))]">Name</dt>
                <dd>{currentOrgQuery.data?.name}</dd>
              </div>
              <div className="flex justify-between gap-4 py-[11px]">
                <dt className="text-[hsl(var(--cx-muted))]">Short name</dt>
                <dd>{currentOrgQuery.data?.slug}</dd>
              </div>
            </dl>
          </SurfaceCard>
        )}
      </Section>

      {gate.org ? (
        <SurfaceCard>
          <div className="flex flex-wrap gap-[7px]">
            <Pill tone={gate.org.has_provider ? "success" : "neutral"}>
              {gate.org.has_provider ? "Provider connected" : "No provider yet"}
            </Pill>
            <Pill tone={gate.org.has_number ? "success" : "neutral"}>
              {gate.org.has_number ? "Number added" : "No number yet"}
            </Pill>
            <Pill tone="info">{gate.org.member_count} {gate.org.member_count === 1 ? "member" : "members"}</Pill>
            <Pill tone="info">{registrationLabel(gate.org.registration_state)}</Pill>
          </div>
        </SurfaceCard>
      ) : null}
    </div>
  );
}

/**
 * Texting registration: 10DLC (a company, per EIN) and toll-free verification (one number).
 * They are separate regimes and are deliberately two panels rather than one wizard.
 *
 * Both self-gate on `compliance:manage` for writes and `compliance:read` for the list, which
 * is exactly what backend/app/api/routes/registration.py requires, so this section does not
 * gate them again - one gate, in the component that owns the request.
 *
 * NOTE for anyone extending this: pressing submit does NOT transmit anything to TCR or a
 * carrier. services/registration.py advances an internal state machine and makes no outbound
 * call; the status only moves when someone POSTs the /status callback. The copy in these
 * panels says so, and there are tests asserting the misleading phrasings never come back.
 */
function MessagingSection() {
  return (
    <div className="space-y-[18px]">
      <TenDlcRegistration />
      <TollFreeVerificationCard />
    </div>
  );
}

function BillingSettingsSection() {
  const tabs = [
    { id: "credits", label: "Credits" },
    { id: "usage", label: "Usage" },
    { id: "dashboard", label: "Dashboard" },
  ];
  const { value, onChange } = useSettingsTab(tabs, "credits");

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
      {value === "credits" ? (
        <CreditsSection />
      ) : value === "usage" ? (
        <BillingUsageSection />
      ) : (
        <DashboardPage />
      )}
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
        <div className="grid gap-[14px]">
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
    case "verification":
      return <VerifyBusinessPage />;
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

/** One class function for every row in the settings nav, so the sections and the routes
 *  that moved here out of the inbox rail cannot drift apart visually. */
function settingsNavLinkClass({ isActive }: { isActive: boolean }): string {
  // The reference's `.nav-item`: 10px radius, 9/10 padding, 13.5px, subtle until it is
  // hovered or current.
  return cn(
    "rounded-[10px] px-[10px] py-[9px] text-[13.5px] transition-colors",
    isActive
      ? "bg-[hsl(var(--cx-overlay))] font-semibold text-[hsl(var(--cx-text))]"
      : "text-[hsl(var(--cx-subtle))] hover:bg-[hsl(var(--cx-overlay))] hover:text-[hsl(var(--cx-text))]",
  );
}

export function SettingsPage() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts: this is a shared store, so the toggle in the sidebar moves
  // every wrapper in the console on the same commit rather than only its own.
  const { theme } = useSurfaceTheme();
  const { section } = useParams<{ section: string }>();
  const gate = useGate();
  const { me, orgId } = useAuth();
  // Ownership, not permission: the owner's "*" role is expanded into every permission
  // string server-side, so an owner and an admin look identical to gate.can(). Read it from
  // the membership (see isOwner). Fail-closed while `me` is null.
  const owner = isOwner(me, orgId);
  const { items: railItems } = useRailNav();
  const current = SETTINGS_SECTIONS.find((s) => s.id === section);

  /**
   * The destinations that moved OUT of the inbox rail and now live here.
   *
   * The operator's rail is Contacts / Campaigns / Settings and nothing else, so Calls and
   * Setup - full routes, not settings sections - have to be reachable from somewhere:
   * this nav is that somewhere. They are read from `useRailNav`, the SAME already-gated
   * list both rails render, and then filtered to whatever the rail does not itself show.
   * Nothing is re-derived, so a member without `calls:read` still gets no Calls row, and
   * Setup still appears only while the checklist has work (see showSetupItem). `/inbox` is
   * excluded by INBOX_RAIL_PATHS' contract.
   *
   * Trust & safety is separate because its gate is `is_platform_operator`, which is not a
   * capability at all - exactly the condition Sidebar and the old rail row used.
   */
  const movedItems = railItems.filter(
    (item) => item.to !== "/inbox" && !INBOX_RAIL_PATHS.includes(item.to),
  );

  if (!section || !current) {
    return <Navigate to="/settings/workspace" replace />;
  }

  const canView = !gate.isLoading && canViewSettingsSection(current, gate.can, owner);

  return (
    <div className={cn(surfaceThemeClass(theme), "flex h-full flex-col overflow-hidden sm:flex-row")}>
      <nav
        aria-label="Settings"
        className="w-full shrink-0 overflow-x-auto border-b border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-3 sm:w-56 sm:overflow-y-auto sm:border-b-0 sm:border-r"
      >
        {gate.isLoading ? (
          <Spinner label="Loading settings" />
        ) : (
          <div className="flex flex-row gap-[3px] sm:flex-col">
            {SETTINGS_SECTIONS.filter((s) => canViewSettingsSection(s, gate.can, owner)).map((s) => (
              <NavLink
                key={s.id}
                to={`/settings/${s.id}`}
                className={settingsNavLinkClass}
              >
                {s.label}
              </NavLink>
            ))}

            {/* The rail's former rows. Separated by a rule and a caption because they are
                not settings SECTIONS - each one leaves this page for a route of its own,
                and the panel beside the nav will not change when you click them. */}
            {movedItems.length > 0 || me?.is_platform_operator ? (
              <>
                <SectionLabel className="hidden px-[10px] pb-2 pt-5 sm:block">
                  More
                </SectionLabel>
                {movedItems.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    aria-label={item.label}
                    className={settingsNavLinkClass}
                  >
                    {item.label}
                  </NavLink>
                ))}
                {me?.is_platform_operator ? (
                  <NavLink
                    to="/ops"
                    aria-label="Trust & safety"
                    className={settingsNavLinkClass}
                  >
                    Trust &amp; safety
                  </NavLink>
                ) : null}
              </>
            ) : null}
          </div>
        )}
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="p-[18px]">
          {gate.isLoading ? (
            <Spinner label="Loading settings" />
          ) : !canView ? (
            <SurfaceCard>
              <p className="text-[13.5px] font-medium">
                You do not have access to this setting.
              </p>
            </SurfaceCard>
          ) : (
            <SectionContent id={current.id} />
          )}
        </div>
      </div>
    </div>
  );
}
