import * as React from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { ConversationsPage } from "@/pages/ConversationsPage";
import { ContactsPage } from "@/pages/ContactsPage";
import { CampaignsPage } from "@/pages/CampaignsPage";
import { CallsPage } from "@/pages/CallsPage";
import { ListsPage } from "@/pages/ListsPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import { LoginPage } from "@/pages/LoginPage";
import { AcceptInvitePage } from "@/pages/AcceptInvitePage";
import { SettingsPage } from "@/pages/SettingsPage";
import { SettingsIndexRedirect } from "@/pages/SettingsIndexRedirect";
import { Spinner } from "@/components/ui/primitives";
import { Sidebar, MobileTabBar } from "@/components/shell/Sidebar";
import { OnboardingChecklist } from "@/components/onboarding/OnboardingChecklist";
import { ErrorBoundary } from "@/components/shell/ErrorBoundary";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import { SoftphonePanel } from "@/softphone/SoftphonePanel";
import { CommandPalette } from "@/components/ui/CommandPalette";

/**
 * Legacy routes kept as redirects so saved links still land somewhere useful:
 * /dashboard, /agent, /appointments, /flows, /queues, /numbers,
 * /providers, /security, /team, /platform, and /inbox/legacy.
 * Every one of these now points into the new /settings surface (or the inbox).
 * /lists stays a live route (no rail entry) — it is still the only UI for
 * list import/management until P27 folds it into Contacts.
 */

function InboxRoute() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <OnboardingChecklist />
      <div className="min-h-0 flex-1">
        <ConversationsPage />
      </div>
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <SoftphoneProvider>
      <div className="dark flex h-full bg-background text-foreground">
        <Sidebar />
        <main className="min-h-0 flex-1 pb-14 sm:pb-0">
          <ErrorBoundary>{children}</ErrorBoundary>
        </main>
      </div>
      <MobileTabBar />
      <CommandPalette />
      <SoftphonePanel />
    </SoftphoneProvider>
  );
}

export function App() {
  const { me, orgId, ready } = useAuth();

  if (!ready) return <Spinner label="Starting" />;

  if (!me) {
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
        <Route path="/inbox" element={<InboxRoute />} />
        <Route path="/inbox/legacy" element={<Navigate to="/inbox" replace />} />
        {/* ConversationsPage still reads the inbox from ?inbox= for now; it will
            start reading :inboxId/:threadId in the next phase. */}
        <Route path="/inbox/:inboxId" element={<InboxRoute />} />
        <Route path="/inbox/:inboxId/:threadId" element={<InboxRoute />} />

        <Route path="/contacts" element={<ContactsPage />} />
        <Route path="/contacts/:contactId" element={<ContactsPage />} />
        <Route path="/calls" element={<CallsPage />} />
        <Route path="/campaigns" element={<CampaignsPage />} />

        <Route path="/settings" element={<SettingsIndexRedirect />} />
        <Route path="/settings/:section" element={<SettingsPage />} />

        <Route path="/accept-invite" element={<AcceptInvitePage />} />

        <Route path="/dashboard" element={<Navigate to="/settings/billing?tab=dashboard" replace />} />
        <Route path="/lists" element={<ListsPage />} />
        <Route path="/agent" element={<Navigate to="/settings/ai" replace />} />
        <Route path="/appointments" element={<Navigate to="/settings/ai" replace />} />
        <Route path="/flows" element={<Navigate to="/settings/calling" replace />} />
        <Route path="/queues" element={<Navigate to="/settings/calling" replace />} />
        <Route path="/numbers" element={<Navigate to="/settings/numbers" replace />} />
        <Route path="/providers" element={<Navigate to="/settings/providers" replace />} />
        <Route path="/security" element={<Navigate to="/settings/team?tab=security" replace />} />
        <Route path="/team" element={<Navigate to="/settings/team" replace />} />
        <Route path="/platform" element={<Navigate to="/settings/developers" replace />} />

        <Route path="*" element={<Navigate to="/inbox" replace />} />
      </Routes>
    </Shell>
  );
}
