import * as React from "react";
import { NavLink } from "react-router-dom";
import { Contact, Inbox, LogOut, Megaphone, Phone, Search, Settings } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { cn } from "@/lib/utils";
import { SETTINGS_SECTIONS } from "@/pages/settingsSections";

type RailItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  permission?: string;
};

export const RAIL_ITEMS: RailItem[] = [
  { to: "/inbox", label: "Inbox", icon: Inbox },
  { to: "/contacts", label: "Contacts", icon: Contact, permission: "contacts:read" },
  { to: "/calls", label: "Calls", icon: Phone, permission: "calls:read" },
  { to: "/campaigns", label: "Campaigns", icon: Megaphone, permission: "campaigns:read" },
];

const MOBILE_ITEMS: RailItem[] = RAIL_ITEMS.filter((i) => i.to !== "/campaigns");
const MOBILE_SETTINGS_ITEM: RailItem = { to: "/settings", label: "Settings", icon: Settings };

function railLinkClass({ isActive }: { isActive: boolean }) {
  return cn(
    "flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted",
    isActive && "bg-muted text-foreground",
  );
}

export function Sidebar() {
  const { me, orgId, selectOrg, logout } = useAuth();
  const gate = useGate();
  const [menuOpen, setMenuOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);

  const org = me?.memberships.find((m) => m.org_id === orgId);
  const canSeeSettings =
    !gate.isLoading && SETTINGS_SECTIONS.some((s) => gate.can(s.permission));

  React.useEffect(() => {
    if (!menuOpen) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        triggerRef.current?.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  const items = gate.isLoading
    ? []
    : RAIL_ITEMS.filter((item) => !item.permission || gate.can(item.permission));

  return (
    <aside className="dark hidden w-14 shrink-0 flex-col items-center border-r border-border bg-background py-2 sm:flex">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Search"
        title="Search (Ctrl K)"
        className="mb-1"
        onClick={() => openCommandPalette()}
      >
        <Search className="h-5 w-5" aria-hidden="true" />
        <span className="sr-only">Search</span>
      </Button>

      {/* P26: the bell sits with Search above the navigation, not inside it - it is not
          a place you go, it is a thing that happened. It renders with or without the
          realtime socket, so a bare <Sidebar /> in a test still works. */}
      <NotificationBell />

      <nav
        aria-label="Sidebar"
        aria-busy={gate.isLoading}
        className="flex flex-1 flex-col items-center gap-1"
      >
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            title={item.label}
            aria-label={item.label}
            className={railLinkClass}
          >
            <item.icon className="h-5 w-5" aria-hidden="true" />
            <span className="sr-only">{item.label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="relative flex flex-col items-center gap-1">
        <Button
          ref={triggerRef}
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Switch workspace"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
        >
          {(org?.org_name ?? "W").slice(0, 1).toUpperCase()}
        </Button>

        {menuOpen ? (
          <div
            role="menu"
            aria-label="Workspaces"
            className="absolute bottom-0 left-14 z-50 w-56 rounded-md border border-border bg-background p-1 shadow-lg"
          >
            {me?.memberships.map((m) => (
              <Button
                key={m.org_id}
                type="button"
                variant="ghost"
                role="menuitem"
                aria-current={m.org_id === orgId ? "true" : undefined}
                className="w-full justify-start font-normal"
                onClick={() => {
                  selectOrg(m.org_id);
                  setMenuOpen(false);
                }}
              >
                {m.org_name}
              </Button>
            ))}
          </div>
        ) : null}

        {canSeeSettings ? (
          <NavLink
            to="/settings"
            title="Settings"
            aria-label="Settings"
            className={railLinkClass}
          >
            <Settings className="h-5 w-5" aria-hidden="true" />
            <span className="sr-only">Settings</span>
          </NavLink>
        ) : null}

        <Button type="button" variant="ghost" size="icon" aria-label="Sign out" onClick={logout}>
          <LogOut className="h-5 w-5" aria-hidden="true" />
          <span className="sr-only">Sign out</span>
        </Button>
      </div>
    </aside>
  );
}

export function MobileTabBar() {
  const gate = useGate();
  const canSeeSettings =
    !gate.isLoading && SETTINGS_SECTIONS.some((s) => gate.can(s.permission));

  const items = gate.isLoading
    ? []
    : [
        ...MOBILE_ITEMS.filter((item) => !item.permission || gate.can(item.permission)),
        ...(canSeeSettings ? [MOBILE_SETTINGS_ITEM] : []),
      ];

  return (
    <nav
      aria-label="Bottom navigation"
      aria-busy={gate.isLoading}
      className="dark fixed inset-x-0 bottom-0 z-30 flex items-center justify-around border-t border-border bg-background py-1 sm:hidden"
    >
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          aria-label={item.label}
          className={({ isActive }) =>
            cn(
              "flex flex-col items-center gap-0.5 rounded-md px-2 py-1 text-muted-foreground hover:bg-muted",
              isActive && "bg-muted text-foreground",
            )
          }
        >
          <item.icon className="h-5 w-5" aria-hidden="true" />
          <span className="text-[11px]">{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
