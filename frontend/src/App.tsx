import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { DashboardPage } from "@/pages/DashboardPage";
import { ConversationsPage } from "@/pages/ConversationsPage";
import { InboxSettingsPage } from "@/pages/InboxSettingsPage";
// F9: kept reachable (never deleted) at its own route - linked under Sidebar "More" as
// "Legacy inbox".
import { InboxPage } from "@/pages/InboxPage";
import { ContactsPage } from "@/pages/ContactsPage";
import { ListsPage } from "@/pages/ListsPage";
import { CampaignsPage } from "@/pages/CampaignsPage";
import { NumbersPage } from "@/pages/NumbersPage";
import { ProvidersPage } from "@/pages/ProvidersPage";
import { CallsPage } from "@/pages/CallsPage";
import { AgentPage } from "@/pages/AgentPage";
import { AppointmentsPage } from "@/pages/AppointmentsPage";
import { FlowsPage } from "@/pages/FlowsPage";
import { QueuesPage } from "@/pages/QueuesPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import { LoginPage } from "@/pages/LoginPage";
import { PlatformPage } from "@/pages/PlatformPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { TeamPage } from "@/pages/TeamPage";
import { AcceptInvitePage } from "@/pages/AcceptInvitePage";
import { Spinner } from "@/components/ui/primitives";
import { Sidebar } from "@/components/shell/Sidebar";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import { SoftphonePanel } from "@/softphone/SoftphonePanel";

/** Replaces the old top nav (plan phase-16-plan.md): the Sidebar is now the one
 * persistent nav frame for every authed route, with the inbox as the app's home. */
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <SoftphoneProvider>
      <div className="flex h-full">
        <Sidebar />
        <main className="min-h-0 flex-1">{children}</main>
      </div>
      <SoftphonePanel />
    </SoftphoneProvider>
  );
}

export function App() {
  const { me, orgId, ready } = useAuth();

  if (!ready) return <Spinner label="Starting" />;

  if (!me) {
    // /accept-invite must be reachable without being logged in - it is how a brand new
    // account gets created. Every other path when unauthenticated falls back to login.
    return (
      <Routes>
        <Route path="/accept-invite" element={<AcceptInvitePage />} />
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }
  if (!orgId) return <OrgPickerPage />;

  return (
    <Shell>
      <Routes>
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/inbox" element={<ConversationsPage />} />
        <Route path="/inbox/legacy" element={<InboxPage />} />
        <Route path="/settings/inboxes" element={<InboxSettingsPage />} />
        <Route path="/contacts" element={<ContactsPage />} />
        <Route path="/lists" element={<ListsPage />} />
        <Route path="/campaigns" element={<CampaignsPage />} />
        <Route path="/calls" element={<CallsPage />} />
        <Route path="/agent" element={<AgentPage />} />
        <Route path="/appointments" element={<AppointmentsPage />} />
        <Route path="/flows" element={<FlowsPage />} />
        <Route path="/queues" element={<QueuesPage />} />
        <Route path="/numbers" element={<NumbersPage />} />
        <Route path="/providers" element={<ProvidersPage />} />
        <Route path="/security" element={<SettingsSecurityPage />} />
        <Route path="/team" element={<TeamPage />} />
        <Route path="/platform" element={<PlatformPage />} />
        <Route path="*" element={<Navigate to="/inbox" replace />} />
      </Routes>
    </Shell>
  );
}
