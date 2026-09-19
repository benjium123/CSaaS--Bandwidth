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
import { SetupPage } from "@/pages/SetupPage";
import { ErrorBoundary, ErrorFallbackNav } from "@/components/shell/ErrorBoundary";
import { BannerRegion } from "@/components/shell/BannerSlot";
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
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { cn } from "@/lib/utils";
// The console palette, and its light half. BOTH belong here rather than beside the one
// component that first needed them.
//
// consoleTheme.css scopes its entire token block to `.console-surface`, and Shell now
// carries that class, so EVERY console page depends on this stylesheet being in the bundle.
// It used to be imported only by ConversationsPage, which worked purely because App imports
// that page statically - the day anyone makes it a `React.lazy` route, the whole console
// palette silently vanishes for every page except the inbox and nothing fails loudly.
// The light file has the same reach for the same reason: the sidebar, the settings pages
// and the command palette are all light too, and none of them render the inbox. Every rule
// in it is scoped to `is-light`, so loading it unconditionally costs the dark theme nothing.
import "@/components/conversations/consoleTheme.css";
import "@/components/conversations/consoleTheme.light.css";

/**
 * Legacy routes kept as redirects so saved links still land somewhere useful:
 * /dashboard, /agent, /appointments, /flows, /queues, /numbers,
 * /providers, /security, /team, /platform, and /inbox/legacy.
 * Every one of these now points into the new /settings surface (or the inbox).
 * P27 folded list import/management into Contacts as its "Lists" tab, so /lists is
 * now a redirect too — saved links land on the tab that replaced the page.
 */

/**
 * The inbox is conversations and nothing else.
 *
 * The setup checklist used to sit here, above the columns. On a new workspace it and a
 * shell banner or two pushed the conversation list into the bottom half of the screen, so
 * clicking Inbox did not show you an inbox. The checklist now lives at /setup, reached from
 * its own entry in the rail - which appears exactly while the checklist has work to show
 * (see isWorkspaceFullySetUp), so a brand-new workspace still finds it.
 */
function InboxRoute() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">
        <ConversationsPage />
      </div>
    </div>
  );
}

/**
 * The inbox rail IS the navigation on /inbox.
 *
 * InboxColumn now renders the console reference's full 240px `.nav` - brand, Search,
 * Notifications, the Workspace group and the Lines group - so the 56px icon Sidebar beside
 * it would be a SECOND copy of the same destinations, two controls disagreeing about which
 * one looks current. One of them had to go, and it is the icon rail, because the merged
 * column is what the operator approved.
 *
 * KEPT HIDDEN after the rail was trimmed back to the approved five items, which is the
 * decision worth writing down. Calls, Setup and Trust & safety came OUT of InboxColumn,
 * and the temptation was to stop hiding the icon rail so they had somewhere to live. That
 * would have put two navigations side by side again - the thing this hiding exists to
 * prevent, and not what the operator's screenshot shows. Instead each of them moved one
 * level down into the Settings surface (SettingsPage's section nav, same useRailNav gate),
 * and the two controls that are not destinations - the theme toggle and Sign out - stay in
 * InboxColumn's cluster beneath the lines. Nothing the icon rail owned is unreachable on
 * /inbox; see the header comment in InboxColumn.tsx for the item-by-item account.
 *
 * Scoped to /inbox ONLY - every other console page still gets the icon rail, which is why
 * this is a path test rather than a deletion.
 *
 * Phones are unaffected: <Sidebar/> is `hidden sm:flex` anyway, and MobileTabBar - the rail
 * on a phone - renders outside this and is untouched.
 */
function useHideIconRail(): boolean {
  const { pathname } = useLocation();
  return pathname === "/inbox" || pathname.startsWith("/inbox/");
}

/** Exported for OnboardingJourney.test.tsx, which pins the console theme class it emits. */
export function Shell({ children }: { children: React.ReactNode }) {
  // The console's theme is the SAME stored preference the front door uses - see
  // useSurfaceTheme. `surfaceThemeClass` still emits the literal `dark` in the dark case,
  // because that class is what index.css's token override and any shadcn `dark:` variant
  // hang off; `is-light` is what consoleTheme.light.css hangs off. The two are mutually
  // exclusive by construction there rather than by discipline at each of the wrappers below.
  const { theme } = useSurfaceTheme();
  const hideIconRail = useHideIconRail();
  // Feeds the error boundary below. Hiding the icon rail on /inbox stays a plain path test
  // and Shell stays ignorant of whether the page under it is healthy: the recovery screen
  // carries navigation of its OWN (see ErrorFallbackNav), which is what restores "a crashed
  // page always leaves you a way out" on every route rather than only on the ones whose
  // chrome happens to live outside the boundary. The pathname doubles as the boundary's
  // reset key - without it a link in the fallback would change the URL and keep rendering
  // the same "Something went wrong", because React never leaves an error state on its own.
  const { pathname } = useLocation();
  return (
    <SoftphoneProvider>
      {/* `console-surface` IS THE THEME SCOPE, and it belongs here rather than on each page.
          consoleTheme.css scopes its entire token block to `.console-surface.console-surface`,
          and that block is what re-points --background/--foreground/--border/--primary/--muted/
          --muted-foreground at the approved reference palette. While the class sat only on
          ConversationsPage, the softphone and the sidebar toggle wrapper, every other console
          page - Contacts, Calls, Campaigns, Settings, Ops - fell outside the scope and resolved
          index.css generic shadcn tokens instead: the old look. Custom properties inherit, so
          carrying it on this one wrapper hands the palette to every page rendered as `children`.
          Public screens (/, /login, /onboarding, /plans) return before this component and are
          not descendants of this node, so no console token can reach them. */}
      <div className={cn("console-surface", surfaceThemeClass(theme), "flex h-full bg-background text-foreground")}>
        {hideIconRail ? null : <Sidebar />}
        <main className="min-h-0 flex-1 pb-14 sm:pb-0">
          {/* At most ONE of these renders - see BannerSlot.tsx for the priority order and
              why it is arbitrated by claim rather than by recomputing four conditions here.
              Order in this JSX is irrelevant to which one wins; it is kept in priority
              order only so it reads the way it behaves. */}
          <BannerRegion>
            <MonitoringBanner />
            <LowBalanceBanner />
            <VerificationBanner />
            <PasskeyGraceBanner />
          </BannerRegion>
          <ErrorBoundary nav={<ErrorFallbackNav />} resetKey={pathname}>
            {children}
          </ErrorBoundary>
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

  // `/plans` joins it, and this was a bug I could only see in a browser. ChoosePlanPage is
  // built on AuthSurface, so routing it INSIDE the Shell rendered an auth surface within the
  // console: a light page inside the dark chrome, with the marketing aside's wordmark
  // bleeding in beside the sidebar. Worse, its breakout is sized in `vw`, and inside the
  // Shell the available width is the viewport MINUS the sidebar - so the third plan card was
  // clipped off the right edge and the page scrolled sideways. Both symptoms, one cause: the
  // page was always an Exchange surface and was being asked to live in console furniture.
  // It belongs beside /onboarding - the screen before it in the journey, and the screen that
  // links here.
  if (location.pathname === "/plans") return <ChoosePlanPage />;

  return (
    <Shell>
      <Routes>
        <Route path="/inbox" element={<InboxRoute />} />
        <Route path="/inbox/legacy" element={<Navigate to="/inbox" replace />} />
        {/* ConversationsPage still reads the inbox from ?inbox= for now; it will
            start reading :inboxId/:threadId in the next phase. */}
        <Route path="/inbox/:inboxId" element={<InboxRoute />} />
        <Route path="/inbox/:inboxId/:threadId" element={<InboxRoute />} />

        <Route path="/setup" element={<SetupPage />} />

        <Route path="/contacts" element={<ContactsPage />} />
        <Route path="/contacts/:contactId" element={<ContactsPage />} />
        <Route path="/calls" element={<CallsPage />} />
        <Route path="/campaigns" element={<CampaignsPage />} />
        <Route path="/ops" element={<OpsPage />} />
        <Route path="/report" element={<ReportNumberPage />} />

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
