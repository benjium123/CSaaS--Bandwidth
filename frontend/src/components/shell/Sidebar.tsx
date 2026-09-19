import * as React from "react";
import { NavLink } from "react-router-dom";
import { Contact, Inbox, LogOut, Megaphone, Phone, Rocket, Search, Settings, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useGate, type Gate } from "@/api/capabilities";
import { isWorkspaceFullySetUp } from "@/components/onboarding/OnboardingChecklist";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { cn } from "@/lib/utils";
import { SETTINGS_SECTIONS } from "@/pages/settingsSections";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { ThemeToggle } from "@/auth/ThemeToggle";

export type RailItem = {
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

/**
 * The only destinations the merged 240px inbox rail lists under "Workspace".
 *
 * The operator's approved rail is brand / Search / Notifications / Workspace (Contacts,
 * Campaigns, Settings) / Lines, and nothing else. Everything else `useRailNav` returns -
 * today Calls and Setup - now lives behind the Settings destination instead, and
 * SettingsPage reads the COMPLEMENT of this list to render it.
 *
 * It is one exported constant rather than two hand-kept lists precisely so a future entry
 * in RAIL_ITEMS cannot fall down the gap between them: anything not named here is picked
 * up by Settings automatically, still carrying whatever permission gate useRailNav applied.
 * `/inbox` is excluded by both sides - the rail IS the inbox, and Settings must not offer a
 * second way back to it.
 */
export const INBOX_RAIL_PATHS: readonly string[] = ["/contacts", "/campaigns"];
/** Settings is not in RAIL_ITEMS because it is gated on "any settings section is visible
 *  to me" rather than on one permission string - see `useRailNav` below. Exported so the
 *  merged inbox rail lists the SAME destination this one does. */
export const SETTINGS_ITEM: RailItem = { to: "/settings", label: "Settings", icon: Settings };
const MOBILE_SETTINGS_ITEM: RailItem = SETTINGS_ITEM;

/**
 * Setup is a destination, not a permission.
 *
 * It is kept out of RAIL_ITEMS because every entry there is gated on a permission string
 * and this one is gated on workspace STATE: it appears exactly while the checklist at
 * /setup has something to show, and disappears the moment the workspace is finished. Both
 * sides of that read `isWorkspaceFullySetUp`, so the rail can never be a dead link to an
 * empty page, nor go missing while there is work on it.
 *
 * No permission gate on purpose: the checklist has never had one either, and adding one
 * here would hide the entry from exactly the members a half-built workspace has most of.
 */
const SETUP_ITEM: RailItem = { to: "/setup", label: "Setup", icon: Rocket };

function showSetupItem(gate: Gate): boolean {
  if (gate.isLoading || gate.org == null) return false;
  return !isWorkspaceFullySetUp(gate.org);
}

/**
 * THE navigation gate, in one place.
 *
 * Sidebar (the 56px icon rail) and InboxColumn (the merged 240px rail the console
 * reference draws on /inbox) render the same destinations, so both must make the same
 * permission decision. They call this rather than each re-deriving it: a rail that showed
 * an item the other hid would be a gating hole found by a user, not by a test.
 *
 * `isLoading` is surfaced rather than swallowed because both rails render NOTHING while
 * capabilities are in flight - a flash of an item a member may not hold is the one thing
 * a gate must never do.
 */
export function useRailNav(): {
  items: RailItem[];
  canSeeSettings: boolean;
  isLoading: boolean;
} {
  const gate = useGate();
  const canSeeSettings =
    !gate.isLoading && SETTINGS_SECTIONS.some((s) => gate.can(s.permission));
  const items = gate.isLoading
    ? []
    : [
        ...RAIL_ITEMS.filter((item) => !item.permission || gate.can(item.permission)),
        // Last in the group rather than first: Inbox stays the thing you land on, and this
        // entry is temporary by design - it leaves once setup is done.
        ...(showSetupItem(gate) ? [SETUP_ITEM] : []),
      ];
  return { items, canSeeSettings, isLoading: gate.isLoading };
}

function railLinkClass({ isActive }: { isActive: boolean }) {
  return cn(
    "flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted",
    isActive && "bg-muted text-foreground",
  );
}

export function Sidebar() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts: this is a shared store, so the toggle in the sidebar moves
  // every wrapper in the console on the same commit rather than only its own.
  const { theme, toggle } = useSurfaceTheme();
  const { me, orgId, selectOrg, logout } = useAuth();
  const { items, canSeeSettings, isLoading: gateLoading } = useRailNav();
  const [menuOpen, setMenuOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);

  const org = me?.memberships.find((m) => m.org_id === orgId);

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

  return (
    <aside className={cn(surfaceThemeClass(theme), "hidden w-14 shrink-0 flex-col items-center border-r border-border bg-background py-2 sm:flex")}>
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
        aria-busy={gateLoading}
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

        {me?.is_platform_operator ? (
          <NavLink
            to="/ops"
            title="Trust & safety"
            aria-label="Trust & safety"
            className={railLinkClass}
          >
            <ShieldCheck className="h-5 w-5" aria-hidden="true" />
            <span className="sr-only">Trust & safety</span>
          </NavLink>
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

        {/* The SAME control the landing page and the auth screens use, driving the SAME
            stored key - see src/auth/useSurfaceTheme.ts. It is 2rem square and sits in the
            bottom cluster with the other utility controls, so it displaces nothing.

            The `console-surface` on the wrapper is load-bearing and is not decoration.
            themeToggle.css is written entirely against `--ex-*` tokens, and the rail is one
            of the console wrappers that carries no palette at all - without this the button
            would resolve every one of those tokens to nothing and render uncoloured. The
            theme class has to be repeated here too, because consoleTheme.light.css's palette
            block is `.console-surface.console-surface.is-light`: a `console-surface` with
            the class only on an ANCESTOR would match the dark block and hand a light rail a
            dark-palette button. */}
        <span className={cn("console-surface", surfaceThemeClass(theme), "contents")}>
          <ThemeToggle theme={theme} onToggle={toggle} />
        </span>

        <Button type="button" variant="ghost" size="icon" aria-label="Sign out" onClick={logout}>
          <LogOut className="h-5 w-5" aria-hidden="true" />
          <span className="sr-only">Sign out</span>
        </Button>
      </div>
    </aside>
  );
}

export function MobileTabBar() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts: this is a shared store, so the toggle in the sidebar moves
  // every wrapper in the console on the same commit rather than only its own.
  const { theme } = useSurfaceTheme();
  const gate = useGate();
  const canSeeSettings =
    !gate.isLoading && SETTINGS_SECTIONS.some((s) => gate.can(s.permission));

  const items = gate.isLoading
    ? []
    : [
        ...MOBILE_ITEMS.filter((item) => !item.permission || gate.can(item.permission)),
        // The mobile bar IS the rail on a phone. Leaving Setup out of it would make the
        // checklist unreachable for a workspace set up from a phone.
        ...(showSetupItem(gate) ? [SETUP_ITEM] : []),
        ...(canSeeSettings ? [MOBILE_SETTINGS_ITEM] : []),
      ];

  return (
    <nav
      aria-label="Bottom navigation"
      aria-busy={gate.isLoading}
      className={cn(surfaceThemeClass(theme), "fixed inset-x-0 bottom-0 z-30 flex items-center justify-around border-t border-border bg-background py-1 sm:hidden")}
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
