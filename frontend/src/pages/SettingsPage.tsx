import * as React from "react";
import { SettingsProfilePage } from "@/pages/SettingsProfilePage";
import { NotificationsSoundPage } from "@/pages/NotificationsSoundPage";
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
import { Search } from "lucide-react";
import { isWorkspaceFullySetUp } from "@/components/onboarding/OnboardingChecklist";
import { TeamPage } from "@/pages/TeamPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { InboxSettingsPage } from "@/pages/InboxSettingsPage";
import { LinesPage } from "@/pages/LinesPage";
import { LineDetailPage } from "@/pages/LineDetailPage";
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
  SETTINGS_GROUPS,
  SETTINGS_MENU_ORDER,
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
  const { api, me } = useAuth();
  const navigate = useNavigate();
  const isOperator = Boolean(me?.is_platform_operator);
  const accountsQuery = useQuery({
    queryKey: ["provider-accounts"],
    queryFn: () => fetchProviderAccounts(api),
    retry: false,
    enabled: isOperator,
  });

  if (!isOperator) {
    return (
      <Section title="Billing & usage">
        <p className="text-sm text-muted-foreground">
          Your usage and charges are shown on the Credits tab.
        </p>
      </Section>
    );
  }

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

function NumbersSection() {
  const [searchParams] = useSearchParams();
  const lineId = searchParams.get("line");
  return lineId ? <LineDetailPage numberId={lineId} /> : <LinesPage />;
}

function SectionContent({ id }: { id: SettingsSectionId }) {
  const { me } = useAuth();
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
      return <NumbersSection />;
    case "providers":
      return me?.is_platform_operator ? (
        <ProvidersPage />
      ) : (
        <Navigate to="/settings/workspace" replace />
      );
    case "messaging":
      return <MessagingSection />;
    case "calling":
      return <CallingSettingsSection />;
    case "ai":
      return <AiSettingsSection />;
    case "billing":
      return <BillingSettingsSection />;
    case "developers":
      return me?.is_platform_operator ? (
        <PlatformPage />
      ) : (
        <SurfaceCard>
          <p className="text-[13.5px] font-medium">You do not have access to this setting.</p>
        </SurfaceCard>
      );
    case "profile":
      return <SettingsProfilePage />;
    case "notifications":
      return <NotificationsSoundPage />;
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
  const [menuQuery, setMenuQuery] = React.useState("");
  const [searchParams] = useSearchParams();

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
            {/* Phase 1c: search, the owner's setup card, then the sections in three groups.
                Gating is unchanged (canViewSettingsSection); operatorOnly sections are only
                LISTED for platform operators - their routes keep their own gates. */}
            <label className="hidden items-center gap-2 rounded-[10px] border border-[hsl(var(--cx-line))] px-[10px] py-[7px] text-[13px] sm:flex">
              <Search className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--cx-subtle))]" aria-hidden="true" />
              <input
                id="settings-search"
                aria-label="Search settings"
                placeholder="Search settings"
                value={menuQuery}
                onChange={(e) => setMenuQuery(e.currentTarget.value)}
                className="min-w-0 flex-1 bg-transparent outline-none"
              />
            </label>
            {owner && gate.org && !isWorkspaceFullySetUp(gate.org) && !menuQuery ? (
              <NavLink
                to="/setup"
                aria-label="Finish setting up your workspace"
                className="mt-2 hidden flex-col rounded-[10px] border border-[hsl(var(--cx-line))] px-[10px] py-[8px] text-[12.5px] hover:bg-[hsl(var(--cx-overlay))] sm:flex"
              >
                {(() => {
                  const org = gate.org!;
                  // No "connect a provider" step: customers never see carriers (we run them).
                  const steps: [boolean, string][] =
                    org.account_type === "individual"
                      ? [[org.has_number, "Get a number"]]
                      : [
                          [org.has_number, "Get a number"],
                          [org.member_count > 1, "Invite your team"],
                          [org.registration_state !== "none", "Register for texting"],
                        ];
                  const done = steps.filter(([ok]) => ok).length;
                  const next = steps.find(([ok]) => !ok)?.[1];
                  return (
                    <>
                      <span className="font-semibold text-[hsl(var(--cx-text))]">
                        Setup {done} of {steps.length} done
                      </span>
                      {next ? (
                        <span className="text-[hsl(var(--cx-accent,var(--cx-subtle)))]">Next: {next}</span>
                      ) : null}
                    </>
                  );
                })()}
              </NavLink>
            ) : null}
            {SETTINGS_GROUPS.map((group) => {
              const q = menuQuery.trim().toLowerCase();
              const rows = SETTINGS_MENU_ORDER.map((id) => SETTINGS_SECTIONS.find((x) => x.id === id))
                .filter((x): x is (typeof SETTINGS_SECTIONS)[number] => Boolean(x))
                .filter((x) => x.group === group.id)
                .filter((x) => canViewSettingsSection(x, gate.can, owner))
                .filter((x) => !x.operatorOnly || me?.is_platform_operator)
                .filter(
                  (x) =>
                    !q ||
                    x.label.toLowerCase().includes(q) ||
                    (x.subPages ?? []).some((sp) => sp.label.toLowerCase().includes(q)),
                );
              if (rows.length === 0) return null;
              return (
                <React.Fragment key={group.id}>
                  <SectionLabel className="hidden px-[10px] pb-1 pt-4 sm:block">{group.label}</SectionLabel>
                  {rows.map((s) => (
                    <React.Fragment key={s.id}>
                      <NavLink to={`/settings/${s.id}`} className={settingsNavLinkClass}>
                        {s.id === "verification" && gate.org?.account_type === "individual"
                          ? "Identity verification"
                          : s.label}
                      </NavLink>
                      {s.id === current.id && s.subPages ? (
                        <div className="ml-[14px] hidden flex-col border-l border-[hsl(var(--cx-line))] pl-2 sm:flex">
                          {s.subPages.map((sp) => (
                            <NavLink
                              key={sp.id}
                              to={`/settings/${s.id}?tab=${sp.id}`}
                              aria-label={`${s.label}: ${sp.label}`}
                              className={() =>
                                cn(
                                  "rounded-[8px] px-2 py-[5px] text-[12.5px]",
                                  (searchParams.get("tab") ?? s.subPages![0].id) === sp.id
                                    ? "font-semibold text-[hsl(var(--cx-text))]"
                                    : "text-[hsl(var(--cx-subtle))] hover:text-[hsl(var(--cx-text))]",
                                )
                              }
                            >
                              {sp.label}
                            </NavLink>
                          ))}
                        </div>
                      ) : null}
                    </React.Fragment>
                  ))}
                </React.Fragment>
              );
            })}

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
