import * as React from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  BarChart3,
  Bell,
  Bot,
  Calendar,
  ChevronDown,
  Contact,
  Hash,
  LayoutGrid,
  ListOrdered,
  ListTodo,
  Megaphone,
  Phone,
  Search,
  Server,
  Settings,
  Shield,
  Users,
  Workflow,
} from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { fetchInboxes, type Inbox } from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const SEARCH_INPUT_SELECTOR = 'input[aria-label="Search conversations"]';

const NAV_ITEMS = [
  { to: "/dashboard", label: "Analytics", icon: BarChart3 },
  { to: "/calls", label: "Activity", icon: Activity },
  { to: "/contacts", label: "Contacts", icon: Contact },
  { to: "/platform", label: "Settings", icon: Settings },
];

// F8: every routed page (see App.tsx) must be reachable from the Sidebar - Lists, AI
// Agent, Appointments, Flows, Queues, and Security were routed but not linked anywhere.
const MORE_ITEMS = [
  { to: "/calls", label: "Calls", icon: Phone },
  { to: "/campaigns", label: "Campaigns", icon: Megaphone },
  { to: "/lists", label: "Lists", icon: ListTodo },
  { to: "/agent", label: "AI Agent", icon: Bot },
  { to: "/appointments", label: "Appointments", icon: Calendar },
  { to: "/flows", label: "Flows", icon: Workflow },
  { to: "/queues", label: "Queues", icon: ListOrdered },
  { to: "/numbers", label: "Numbers", icon: Hash },
  { to: "/providers", label: "Providers", icon: Server },
  { to: "/security", label: "Security", icon: Shield },
  { to: "/team", label: "Team", icon: Users },
  { to: "/platform", label: "Platform", icon: LayoutGrid },
  { to: "/settings/inboxes", label: "Inboxes & departments", icon: Settings },
  // F9: the pre-P16 inbox stays reachable (never deleted) at its own route.
  { to: "/inbox/legacy", label: "Legacy inbox", icon: Archive },
];

const INBOXES_OPEN_KEY = "csaas.sidebar.inboxes.open";
const MORE_OPEN_KEY = "csaas.sidebar.more.open";

function useSectionCollapse(key: string): [boolean, (open: boolean) => void] {
  const [open, setOpen] = React.useState<boolean>(() => localStorage.getItem(key) !== "false");
  React.useEffect(() => {
    localStorage.setItem(key, String(open));
  }, [key, open]);
  return [open, setOpen];
}

function SectionHeader({
  open,
  onToggle,
  label,
}: {
  open: boolean;
  onToggle: (open: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      onClick={() => onToggle(!open)}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-neutral-500 hover:text-neutral-300"
    >
      <ChevronDown
        className={cn("h-3.5 w-3.5 transition-transform", !open && "-rotate-90")}
      />
      {label}
    </button>
  );
}

function InboxRow({
  inbox,
  active,
  onNavigate,
}: {
  inbox: Inbox;
  active: boolean;
  onNavigate: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-300 hover:bg-neutral-900",
        active && "bg-neutral-800 text-neutral-50",
      )}
    >
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: inbox.color || "#737373" }}
        aria-hidden="true"
      />
      <span className="min-w-0 flex-1 truncate text-left">{inbox.name}</span>
      <span className="shrink-0 text-[10px] tabular-nums text-neutral-500">
        {formatPhone(inbox.e164)}
      </span>
    </button>
  );
}

export function Sidebar() {
  const { me, orgId, api, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [inboxesOpen, setInboxesOpen] = useSectionCollapse(INBOXES_OPEN_KEY);
  const [moreOpen, setMoreOpen] = useSectionCollapse(MORE_OPEN_KEY);

  const org = me?.memberships.find((m) => m.org_id === orgId);
  const { data: inboxes, isLoading, error } = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });

  const items = inboxes ?? [];
  const hasAdminInbox = items.some((inbox) => inbox.my_role === "admin");
  const activeInboxId = React.useMemo(() => {
    const params = new URLSearchParams(location.search);
    return params.get("inbox");
  }, [location.search]);

  // F21: department grouping of inboxes is deferred - GET /api/v1/inboxes carries no
  // department field to group by (see backend/app/api/routes/inboxes.py InboxOut), so
  // inboxes render as one flat admin-visible list until that's added.

  function goToInboxSearch() {
    if (location.pathname === "/inbox") {
      document.querySelector<HTMLInputElement>(SEARCH_INPUT_SELECTOR)?.focus();
      return;
    }
    navigate("/inbox");
    // The conversation list (and its search box) only mounts once /inbox has rendered -
    // give the route change a beat to land before trying to focus it.
    setTimeout(() => {
      document.querySelector<HTMLInputElement>(SEARCH_INPUT_SELECTOR)?.focus();
    }, 50);
  }

  return (
    <aside className="dark flex h-full w-[280px] shrink-0 flex-col border-r border-neutral-800 bg-neutral-950 text-neutral-100">
      <div className="flex items-center gap-3 border-b border-neutral-800 px-3 py-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-neutral-800 text-sm font-semibold text-neutral-200">
          {(org?.org_name ?? "W").slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-neutral-50">
            {org?.org_name ?? "Workspace"}
          </p>
          <p className="truncate text-[11px] text-neutral-500">{orgId}</p>
        </div>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2 py-3" aria-label="Sidebar">
        <div className="space-y-0.5">
          {/* F21: "Search" always means the inbox's conversation search, not a page. */}
          <button
            type="button"
            onClick={goToInboxSearch}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-neutral-300 hover:bg-neutral-900"
          >
            <Search className="h-4 w-4 shrink-0" />
            <span>Search</span>
          </button>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.label}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-300 hover:bg-neutral-900",
                  isActive && "bg-neutral-800 text-neutral-50",
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </div>

        <div className="mt-5">
          <SectionHeader
            label="Inboxes"
            open={inboxesOpen}
            onToggle={setInboxesOpen}
          />
          {inboxesOpen && (
            <div className="mt-1 space-y-0.5">
              {hasAdminInbox && (
                <button
                  type="button"
                  onClick={() => navigate("/inbox?inbox=all")}
                  aria-current={activeInboxId === "all" ? "page" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-300 hover:bg-neutral-900",
                    activeInboxId === "all" && "bg-neutral-800 text-neutral-50",
                  )}
                >
                  <Bell className="h-4 w-4 shrink-0" />
                  <span>All inboxes</span>
                </button>
              )}

              {isLoading ? (
                <p className="px-2 py-1 text-xs text-neutral-500">Loading inboxes…</p>
              ) : error ? (
                <p className="px-2 py-1 text-xs text-red-400">Failed to load inboxes</p>
              ) : (
                items.map((inbox) => (
                  <InboxRow
                    key={inbox.id}
                    inbox={inbox}
                    active={activeInboxId === inbox.id}
                    onNavigate={() => navigate(`/inbox?inbox=${inbox.id}`)}
                  />
                ))
              )}
            </div>
          )}
        </div>

        <div className="mt-5">
          <SectionHeader label="More" open={moreOpen} onToggle={setMoreOpen} />
          {moreOpen && (
            <div className="mt-1 space-y-0.5">
              {MORE_ITEMS.map((item) => (
                <NavLink
                  key={item.label}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-300 hover:bg-neutral-900",
                      isActive && "bg-neutral-800 text-neutral-50",
                    )
                  }
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
          )}
        </div>
      </nav>

      <div className="border-t border-neutral-800 px-2 py-2">
        <button
          type="button"
          onClick={logout}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100"
        >
          Sign out
        </button>
      </div>
    </aside>
  );
}
