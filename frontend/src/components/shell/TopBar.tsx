/**
 * The console's top bar: the 52px band that replaces the icon rail's utility column on a
 * real screen. Phones keep MobileTabBar - this is `hidden sm:flex` on purpose.
 *
 * The navigation here is the SAME one the rail renders, taken from useRailNav rather than
 * re-derived, so the two can never disagree about what a member may see. Settings is
 * appended when it is visible, exactly as the rail does.
 */
import * as React from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchUnreadByInbox } from "@/api/conversations";
import { ChevronDown, Search } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { SETTINGS_ITEM, useRailNav } from "@/components/shell/Sidebar";
import { Button } from "@/components/ui/primitives";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { StatusPill } from "@/components/shell/StatusPill";
import { useOptionalSoftphone } from "@/softphone/SoftphoneProvider";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { HelpMenu } from "@/components/shell/HelpMenu";
import { ThemeToggle } from "@/auth/ThemeToggle";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { SupportUnreadPill } from "@/components/shell/SupportUnreadPill";
import { cn } from "@/lib/utils";

function initials(me: { full_name?: string | null; email?: string | null } | null | undefined): string {
  const name = me?.full_name?.trim();
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean);
    const first = parts[0]?.[0] ?? "";
    const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
    const value = `${first}${last}`.toUpperCase();
    return value || "?";
  }
  const fromEmail = me?.email?.trim()[0];
  return (fromEmail ?? "?").toUpperCase();
}

export function TopBar({ inboxUnread }: { inboxUnread?: number }) {
  const { api, me, orgId, selectOrg, logout } = useAuth();
  // Same key and cadence as ConversationsPage, so the badge shares its cache instead of
  // polling twice. A member without inbox access gets an error here and simply no badge.
  const unreadQuery = useQuery({
    queryKey: ["inbox-unread-counts"],
    queryFn: () => fetchUnreadByInbox(api),
    enabled: Boolean(me && orgId) && inboxUnread === undefined,
    staleTime: 5000,
    refetchInterval: 15000,
  });
  const unreadTotal =
    inboxUnread ?? Object.values(unreadQuery.data?.counts ?? {}).reduce((sum, n) => sum + n, 0);
  const org = me?.memberships.find((m) => m.org_id === orgId);
  const [wsOpen, setWsOpen] = React.useState(false);
  const wsRef = React.useRef<HTMLButtonElement | null>(null);
  const wsMenuRef = React.useRef<HTMLDivElement | null>(null);
  const { theme, toggle } = useSurfaceTheme();
  const { items, canSeeSettings, isLoading } = useRailNav();
  const [accountOpen, setAccountOpen] = React.useState(false);
  const accountRef = React.useRef<HTMLButtonElement | null>(null);
  const accountMenuRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!accountOpen) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setAccountOpen(false);
        accountRef.current?.focus();
      }
    };

    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (accountMenuRef.current?.contains(target)) return;
      if (accountRef.current?.contains(target)) return;
      setAccountOpen(false);
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [accountOpen]);

  React.useEffect(() => {
    if (!wsOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setWsOpen(false);
        wsRef.current?.focus();
      }
    };
    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target || wsMenuRef.current?.contains(target) || wsRef.current?.contains(target)) return;
      setWsOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [wsOpen]);

  // Trust & safety (platform operators only) was a rail link; it stays one click away here.
  const navItems = [
    ...items,
    ...(canSeeSettings ? [SETTINGS_ITEM] : []),
    ...(me?.is_platform_operator ? [{ to: "/ops", label: "Trust & safety" }] : []),
  ];

  return (
    <header
      aria-label="Top bar"
      className="hidden h-[52px] w-full items-center gap-2 border-b border-border bg-background px-3 sm:flex"
    >
      <NavLink to="/inbox" aria-label="Ringlite home" className="flex items-center">
        <span className="ri-wordmark">ringlite</span>
      </NavLink>

      {/* Moved from the icon rail: same names, same menu, same selectOrg. */}
      <div className="relative">
        <Button
          ref={wsRef}
          type="button"
          variant="ghost"
          aria-label="Switch workspace"
          aria-haspopup="menu"
          aria-expanded={wsOpen}
          className="max-w-[200px] gap-1 text-sm"
          onClick={() => setWsOpen((value) => !value)}
        >
          <span className="truncate">{org?.org_name ?? "Workspace"}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        </Button>
        {wsOpen ? (
          <div
            ref={wsMenuRef}
            role="menu"
            aria-label="Workspaces"
            className="absolute left-0 z-50 mt-1 w-56 rounded-md border border-border bg-background p-1 shadow-lg"
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
                  setWsOpen(false);
                }}
              >
                {m.org_name}
              </Button>
            ))}
          </div>
        ) : null}
      </div>

      <nav aria-label="Main" aria-busy={isLoading} className="flex flex-1 items-center gap-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            aria-label={item.label}
            className={({ isActive }) =>
              cn(
                "relative flex items-center rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-muted",
                isActive && "bg-muted text-foreground",
              )
            }
          >
            <span>{item.label}</span>
            {item.to === "/inbox" && unreadTotal > 0 ? (
              <span
                aria-label={`${unreadTotal} unread`}
                className="ml-1 rounded-full bg-destructive px-1.5 text-[11px] font-medium text-background"
              >
                {unreadTotal}
              </span>
            ) : null}
          </NavLink>
        ))}
      </nav>

      <Button
        type="button"
        variant="ghost"
        aria-label="Search"
        className="gap-2"
        onClick={() => openCommandPalette()}
      >
        <Search className="h-4 w-4" aria-hidden="true" />
        <span>Search</span>
        <kbd className="rounded border border-border px-1 text-[11px] text-muted-foreground">
          Ctrl K
        </kbd>
      </Button>

      <div className="flex items-center gap-1">
        <SupportUnreadPill />
        <LiveCallPill />
        <StatusPill />
        <NotificationBell />
        <HelpMenu placement="topbar" />
        {/* Light / dark, always visible (was a rail button). The console-surface wrapper is
            load-bearing for themeToggle.css tokens - see the note in Sidebar.tsx. */}
        <span className={cn("console-surface", surfaceThemeClass(theme), "contents")}>
          <ThemeToggle theme={theme} onToggle={toggle} />
        </span>
        <div className="relative">
          <Button
            ref={accountRef}
            type="button"
            variant="ghost"
            aria-label="Account"
            aria-haspopup="menu"
            aria-expanded={accountOpen}
            onClick={() => setAccountOpen((value) => !value)}
          >
            {initials(me)}
          </Button>

          {accountOpen ? (
            <div
              ref={accountMenuRef}
              role="menu"
              aria-label="Account"
              className="absolute right-0 z-50 mt-1 w-56 rounded-md border border-border bg-background p-2 shadow-lg"
            >
              <Button
                type="button"
                variant="ghost"
                role="menuitem"
                className="w-full justify-start font-normal"
                onClick={() => {
                  setAccountOpen(false);
                  logout();
                }}
              >
                Sign out
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}

/** "On a call · 03:12" while a call is live, on every page (spec §1). */
function LiveCallPill() {
  const softphone = useOptionalSoftphone();
  const call = softphone?.activeCall ?? null;
  const [startedAt, setStartedAt] = React.useState<number | null>(null);
  const [, setTick] = React.useState(0);
  React.useEffect(() => {
    if (!call) {
      setStartedAt(null);
      return undefined;
    }
    setStartedAt(Date.now());
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [call?.id]);
  if (!call || startedAt === null) return null;
  const secs = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const mmss = `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
  return (
    <span
      role="status"
      aria-label={`On a call with ${call.contact}, ${mmss}`}
      className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-semibold text-emerald-600 dark:text-emerald-400"
    >
      <span aria-hidden="true" className="h-2 w-2 rounded-full bg-emerald-500" />
      On a call
      <span className="font-mono font-normal">{mmss}</span>
    </span>
  );
}
