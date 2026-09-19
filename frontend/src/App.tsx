import * as React from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { ConversationsPage } from "@/pages/ConversationsPage";
import { ContactsPage } from "@/pages/ContactsPage";
import { CampaignsPage } from "@/pages/CampaignsPage";
import { CallsPage } from "@/pages/CallsPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import { LoginPage } from "@/pages/LoginPage";
import { SsoCallbackPage } from "@/pages/SsoCallbackPage";
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
import { LowBalanceBanner } from "@/components/billing/LowBalanceBanner";
import { VerificationBanner } from "@/components/kyc/VerificationBanner";
import { MonitoringBanner } from "@/components/kyc/MonitoringBanner";
import { ReportNumberPage } from "@/pages/ReportNumberPage";
import { StepUpDialog } from "@/components/security/StepUpDialog";
import { SecureAccountPage } from "@/pages/SecureAccountPage";
import { OpsPage } from "@/pages/OpsPage";
import { ForgotPasswordPage, ResetPasswordPage } from "@/pages/PasswordResetPages";
import { RecoverAccountPage } from "@/pages/RecoverAccountPage";
import { SignUpPage } from "@/pages/SignUpPage";
import { OnboardingPage } from "@/pages/OnboardingPage";
import { LandingPage } from "@/pages/LandingPage";
import { ChoosePlanPage } from "@/pages/ChoosePlanPage";
import { PasskeyGraceBanner } from "@/components/security/PasskeyGraceBanner";

/**
 * Legacy routes kept as redirects so saved links still land somewhere useful:
 * /dashboard, /agent, /appointments, /flows, /queues, /numbers,
 * /providers, /security, /team, /platform, and /inbox/legacy.
 * Every one of these now points into the new /settings surface (or the inbox).
 * P27 folded list import/management into Contacts as its "Lists" tab, so /lists is
 * now a redirect too — saved links land on the tab that replaced the page.
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
          <PasskeyGraceBanner />
          <VerificationBanner />
          <MonitoringBanner />
          <LowBalanceBanner />
          <ErrorBoundary>{children}</ErrorBoundary>
        </main>
      </div>
      <MobileTabBar />
      <CommandPalette />
      <SoftphonePanel />
      <StepUpDialog />
    </SoftphoneProvider>
  );
}

export function App() {
  const { me, orgId, ready } = useAuth();
  const location = useLocation();

  if (!ready) return <Spinner label="Starting" />;

  if (!me) {
    return (
      <Routes>
        <Route path="/accept-invite" element={<AcceptInvitePage />} />
        <Route path="/auth/sso/callback" element={<SsoCallbackPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />
        {/* The public face. `/` is the landing page and sign-in has moved to its own
            path, so every "back to sign in" link points at /login. The `*` fallback stays
            LoginPage: a deep link into the console by someone signed out should land on the
            sign-in form, not on marketing. */}
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignUpPage />} />
        <Route path="/recover" element={<RecoverAccountPage />} />
        <Route path="/report" element={<ReportNumberPage />} />
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }

  // P41: an account without an authenticator app or passkey can do nothing else yet.
  if (me.second_factor_required) return <SecureAccountPage />;

  if (!orgId) {
    // P43: platform operators often belong to no workspace - the review console must not
    // be hidden behind the workspace picker.
    if (me.is_platform_operator) {
      return (
        <>
          <Routes>
            <Route path="/ops" element={<main className="mx-auto max-w-6xl p-4 sm:p-6"><OpsPage /></main>} />
            <Route path="/report" element={<ReportNumberPage />} />
            <Route path="*" element={<OrgPickerPage />} />
          </Routes>
          <StepUpDialog />
        </>
      );
    }
    return <OrgPickerPage />;
  }

  // The verification journey is its own full-screen surface rather than a page inside the
  // console shell: it is the continuation of signing up, and it carries the same Exchange
  // furniture as /signup and the second-factor wall, so the three read as one journey.
  // Deliberately NOT an interstitial - an open application does not stop the rest of the
  // workspace working, and the screen itself says so.
  if (location.pathname === "/onboarding") return <OnboardingPage />;

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
        <Route path="/ops" element={<OpsPage />} />
        <Route path="/report" element={<ReportNumberPage />} />

        {/* Plan selection. Inside the Shell rather than on the auth surface: by the time
            anyone sees this they have a workspace and are signed in, and it must stay
            escapable - choosing a plan is a task, not a wall. */}
        <Route path="/plans" element={<ChoosePlanPage />} />
        <Route path="/settings" element={<SettingsIndexRedirect />} />
        <Route path="/settings/:section" element={<SettingsPage />} />

        <Route path="/accept-invite" element={<AcceptInvitePage />} />

        <Route path="/auth/sso/callback" element={<SsoCallbackPage />} />

        <Route path="/dashboard" element={<Navigate to="/settings/billing?tab=dashboard" replace />} />
        <Route path="/lists" element={<Navigate to="/contacts?tab=lists" replace />} />
        <Route path="/agent" element={<Navigate to="/settings/ai" replace />} />
        <Route path="/appointments" element={<Navigate to="/settings/ai" replace />} />
        <Route path="/flows" element={<Navigate to="/settings/calling?tab=flows" replace />} />
        <Route path="/queues" element={<Navigate to="/settings/calling?tab=queues" replace />} />
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
