/**
 * The console's top bar: the 56px band that replaces the icon rail's utility column on a
 * real screen. Phones keep MobileTabBar - this is `hidden sm:flex` on purpose.
 *
 * The navigation here is the SAME one the rail renders, taken from useRailNav rather than
 * re-derived, so the two can never disagree about what a member may see. Settings is
 * appended when it is visible, exactly as the rail does.
 *
 * It is deliberately BOLDER than the rail it replaced: 56px tall, every destination drawn as
 * an icon AND its name, and the current one marked with a filled pill *and* an underline. The
 * icon rail could only show the icon, so a destination had no name until you hovered it. The
 * price of names is width, and the nav pays it: it is the only thing here that scrolls
 * sideways, so a member with every permission never gets a page-wide horizontal scrollbar.
 */
import * as React from "react";
import { Link, NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchUnreadByInbox } from "@/api/conversations";
import { ChevronDown, Radio, Search, ShieldCheck } from "lucide-react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { SETTINGS_ITEM, useRailNav, type RailItem } from "@/components/shell/Sidebar";
import { Button } from "@/components/ui/primitives";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { StatusPill } from "@/components/shell/StatusPill";
import { useOptionalSoftphone } from "@/softphone/SoftphoneProvider";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { HelpMenu } from "@/components/shell/HelpMenu";
import { ThemeToggle } from "@/auth/ThemeToggle";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { SupportUnreadPill } from "@/components/shell/SupportUnreadPill";
import { LiveCallsSupervisor } from "@/components/calls/LiveCallsSupervisor";
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

  // Trust & safety (platform operators only) was a rail link; it stays one click away here,
  // and carries the same shield the rail gave it.
  const navItems: RailItem[] = [
    ...items,
    ...(canSeeSettings ? [SETTINGS_ITEM] : []),
    ...(me?.is_platform_operator
      ? [{ to: "/ops", label: "Trust & safety", icon: ShieldCheck }]
      : []),
  ];

  // Live-call supervision used to be the rail's Calls entry. Calls are the inbox's job now,
  // but the people who hold this permission watch calls WHILE they work, so it is a panel.
  const canSupervise = hasPermission(me, orgId, "calls:supervise");

  return (
    <header
      aria-label="Top bar"
      className="hidden h-14 w-full min-w-0 items-center gap-2 border-b border-border bg-background px-4 sm:flex"
    >
      <NavLink to="/inbox" aria-label="Ringlite home" className="flex shrink-0 items-center">
        <span className="ri-wordmark">ringlite</span>
      </NavLink>

      {/* Moved from the icon rail: same names, same menu, same selectOrg. */}
      <div className="relative min-w-0 shrink-0">
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

      {/* The only horizontally scrolling thing on the page, so long names never push the
          layout sideways. `min-w-0` is what lets a flex child actually shrink to scroll. */}
      <nav
        aria-label="Main"
        aria-busy={isLoading}
        className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
      >
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            aria-label={item.label}
            className={({ isActive }) =>
              cn(
                "relative flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium text-foreground/80 transition-colors hover:bg-muted hover:text-foreground",
                isActive &&
                  "bg-primary/15 font-semibold text-primary after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary after:content-['']",
              )
            }
          >
            <item.icon className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{item.label}</span>
            {item.to === "/inbox" && unreadTotal > 0 ? (
              <span
                aria-label={`${unreadTotal} unread`}
                className="ml-1 inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-destructive px-1.5 text-xs font-semibold text-white"
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
        className="shrink-0 gap-2"
        onClick={() => openCommandPalette()}
      >
        <Search className="h-4 w-4" aria-hidden="true" />
        <span>Search</span>
        <kbd className="rounded border border-border px-1 text-[11px] text-muted-foreground">
          Ctrl K
        </kbd>
      </Button>

      <div className="flex min-w-0 shrink-0 items-center gap-2">
        <SupportUnreadPill />
        {canSupervise ? <LiveCallsButton /> : null}
        <LiveCallPill />
        <StatusPill />
        {/* Each utility control keeps `text-foreground` rather than the muted tint it would
            otherwise inherit, and names itself on hover. */}
        <div title="Notifications" className="text-foreground">
          <NotificationBell placement="topbar" />
        </div>
        <div title="Help" className="text-foreground">
          <HelpMenu placement="topbar" />
        </div>
        {/* Light / dark, always visible (was a rail button). The console-surface wrapper is
            load-bearing for themeToggle.css tokens - see the note in Sidebar.tsx. */}
        <span
          title="Theme"
          className={cn("console-surface", surfaceThemeClass(theme), "text-foreground")}
        >
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
              <div className="px-3 pb-2 pt-1">
                <p className="truncate text-sm font-semibold text-foreground">{me?.full_name || me?.email}</p>
                {me?.full_name ? <p className="truncate text-xs text-muted-foreground">{me.email}</p> : null}
              </div>
              <Link
                to="/settings/profile"
                role="menuitem"
                className="flex w-full flex-col rounded-md px-3 py-2 text-sm text-foreground hover:bg-muted"
                onClick={() => setAccountOpen(false)}
              >
                <span>Edit profile</span>
                <span className="text-xs text-muted-foreground">Name and 911 address</span>
              </Link>
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

/**
 * Live-call supervision, folded into the top bar (§1).
 *
 * It was the rail's Calls entry. The inbox's own Calls tab replaced the page, so the only
 * thing left of it a supervisor still needs is the live view - and they need that WHILE they
 * work, not on a page they have to leave the inbox to reach. The panel hangs right-aligned
 * under the button, like the other top-right menus.
 */
function LiveCallsButton() {
  const [open, setOpen] = React.useState(false);
  const wrapperRef = React.useRef<HTMLDivElement | null>(null);
  const buttonRef = React.useRef<HTMLButtonElement | null>(null);

  React.useEffect(() => {
    if (!open) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    const onMouseDown = (event: MouseEvent) => {
      if (wrapperRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [open]);

  return (
    <div ref={wrapperRef} className="relative">
      <Button
        ref={buttonRef}
        type="button"
        variant="ghost"
        aria-label="Live"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="gap-1.5"
        onClick={() => setOpen((value) => !value)}
      >
        <Radio className="h-4 w-4" aria-hidden="true" />
        <span>Live</span>
      </Button>

      {open ? (
        <div
          role="dialog"
          aria-label="Live calls"
          className="absolute right-0 top-full mt-2 z-50 w-[420px] max-w-[calc(100vw-1rem)] max-h-[70vh] overflow-y-auto rounded-md border border-border bg-background p-2 shadow-lg"
        >
          <LiveCallsSupervisor />
        </div>
      ) : null}
    </div>
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
