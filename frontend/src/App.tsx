import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { InboxPage } from "@/pages/InboxPage";
import { ContactsPage } from "@/pages/ContactsPage";
import { NumbersPage } from "@/pages/NumbersPage";
import { CallsPage } from "@/pages/CallsPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import { LoginPage } from "@/pages/LoginPage";
import { SettingsSecurityPage } from "@/pages/SettingsSecurityPage";
import { Button, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/inbox", label: "Inbox" },
  { to: "/contacts", label: "Contacts" },
  { to: "/calls", label: "Calls" },
  { to: "/numbers", label: "Numbers" },
  { to: "/security", label: "Security" },
];

function Shell({ children }: { children: React.ReactNode }) {
  const { me, orgId, logout } = useAuth();
  const org = me?.memberships.find((m) => m.org_id === orgId);
  return (
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
    </div>
  );
}

export function App() {
  const { me, orgId, ready } = useAuth();

  if (!ready) return <Spinner label="Starting" />;
  if (!me) return <LoginPage />;
  if (!orgId) return <OrgPickerPage />;

  return (
    <Shell>
      <Routes>
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/contacts" element={<ContactsPage />} />
        <Route path="/calls" element={<CallsPage />} />
        <Route path="/numbers" element={<NumbersPage />} />
        <Route path="/security" element={<SettingsSecurityPage />} />
        <Route path="*" element={<Navigate to="/inbox" replace />} />
      </Routes>
    </Shell>
  );
}
