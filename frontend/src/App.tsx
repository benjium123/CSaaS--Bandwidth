import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { InboxPage } from "@/pages/InboxPage";
import { ContactsPage } from "@/pages/ContactsPage";
import { NumbersPage } from "@/pages/NumbersPage";
import { ProvidersPage } from "@/pages/ProvidersPage";
import { CallsPage } from "@/pages/CallsPage";
import { AgentPage } from "@/pages/AgentPage";
import { AppointmentsPage } from "@/pages/AppointmentsPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import { LoginPage } from "@/pages/LoginPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { TeamPage } from "@/pages/TeamPage";
import { AcceptInvitePage } from "@/pages/AcceptInvitePage";
import { Button, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";
import { SoftphonePanel } from "@/softphone/SoftphonePanel";

const NAV = [
  { to: "/inbox", label: "Inbox" },
  { to: "/contacts", label: "Contacts" },
  { to: "/calls", label: "Calls" },
  { to: "/agent", label: "AI Agent" },
  { to: "/appointments", label: "Appointments" },
  { to: "/numbers", label: "Numbers" },
  { to: "/providers", label: "Providers" },
  { to: "/security", label: "Security" },
  { to: "/team", label: "Team" },
];

function Shell({ children }: { children: React.ReactNode }) {
  const { me, orgId, logout } = useAuth();
  const org = me?.memberships.find((m) => m.org_id === orgId);
  return (
    <SoftphoneProvider>
      <div className="grid h-full grid-rows-[auto_1fr]">
        <header className="flex items-center justify-between gap-4 border-b border-border px-4 py-2">
          <nav className="flex items-center gap-1">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to}>
                {({ isActive }) => (
                  <span
                    className={cn(
                      "rounded-md px-3 py-1.5 text-sm",
                      isActive ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {n.label}
                  </span>
                )}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>{org?.org_name}</span>
            <Button size="sm" variant="ghost" onClick={logout}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="min-h-0">{children}</main>
        <SoftphonePanel />
      </div>
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
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/contacts" element={<ContactsPage />} />
        <Route path="/calls" element={<CallsPage />} />
        <Route path="/agent" element={<AgentPage />} />
        <Route path="/appointments" element={<AppointmentsPage />} />
        <Route path="/numbers" element={<NumbersPage />} />
        <Route path="/providers" element={<ProvidersPage />} />
        <Route path="/security" element={<SettingsSecurityPage />} />
        <Route path="/team" element={<TeamPage />} />
        <Route path="*" element={<Navigate to="/inbox" replace />} />
      </Routes>
    </Shell>
  );
}
