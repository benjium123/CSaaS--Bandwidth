import * as React from "react";
import { NavLink } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  DndContext,
  PointerSensor,
  KeyboardSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  GripVertical,
  PanelLeftClose,
  LogOut,
  MessageSquare,
  Phone,
  Plus,
  Search,
  ShieldCheck,
  Users2,
} from "lucide-react";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { putInboxOrder, type Inbox } from "@/api/conversations";
import type { NewConversationKind } from "@/components/conversations/NewConversationPanel";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { INBOX_RAIL_PATHS, SETTINGS_ITEM, useRailNav } from "@/components/shell/Sidebar";
import { ThemeToggle } from "@/auth/ThemeToggle";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { NumberAccessDrawer } from "@/components/conversations/NumberAccessDrawer";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The rail is the console reference's `<nav class="nav">` - the 240px column that is the
 * workspace navigation AND the line picker in one. See docs/design/console-reference.html.
 *
 * It used to be scope-only, with the navigation living in the 56px icon Sidebar beside it.
 * The reference merges the two, so App.tsx hides <Sidebar/> on /inbox (desktop only -
 * MobileTabBar is untouched and is still the rail on a phone). That makes this component
 * the ONLY navigation on the inbox route.
 *
 * WHAT IT LISTS, AND WHY NOT MORE. The approved rail is exactly: brand, Search,
 * Notifications, "Workspace" (Contacts, Campaigns, Settings), "Lines", then the settings /
 * account cluster beneath the numbers. An earlier pass also carried Calls, Setup and
 * Trust & safety here, on the reasoning that the icon rail was hidden and they would
 * otherwise be unreachable. That reasoning was right about the risk and wrong about the
 * fix: they now live one level down, inside the Settings surface (see SettingsPage's
 * section nav, which reads the COMPLEMENT of INBOX_RAIL_PATHS from the same useRailNav
 * gate). So none of them is stranded, and the rail stays the five things the operator
 * asked for.
 *
 * The theme toggle and Sign out are NOT destinations and cannot move into a page's nav, so
 * they stay in the cluster below the lines - that cluster IS the "settings access below the
 * phone numbers". Deleting them would strand two controls on the console's busiest page.
 *
 * Permission gating is NOT re-derived here. `useRailNav` in Sidebar.tsx is the one gate
 * both rails read, so an item hidden from a member in one is hidden in both - and the
 * items that moved into Settings are filtered out of the SAME already-gated list, so a
 * member without `calls:read` gains nothing by the move.
 *
 * The Important / Unresponded / Snoozed / Overdue "view" rows that used to live here were
 * removed against the same reference: each one set the very same `filter` value as a chip
 * two columns over, so one piece of state had two controls that could disagree about which
 * of them looked selected. The chips in ConversationList are the only entry point to a
 * filter. Snoozing a conversation and the overdue badge are untouched.
 */
export type InboxColumnSelection =
  | { kind: "all" }
  | { kind: "inbox"; inboxId: string };

/**
 * Two letters for the line avatar, per the reference: Main line -> MN, Support -> SP,
 * Sales -> SL. That is NOT word initials ("Main line" would give ML); it is the first
 * letter plus the next consonant, which is what all three of the mockup's avatars are.
 *
 * The mockup's third avatar reads UK for a +44 number, i.e. derived from the country
 * rather than the name. `Inbox` (src/api/conversations.ts) carries no country field, and
 * the only other candidate is the e164 prefix, which would mean hand-rolling a dialling
 * code to country table here. So this derives from the NAME in every case, including the
 * international one. If a country field ever lands on Inbox, prefer it here.
 *
 * Deliberately NOT `initialsOf` from src/lib/format.ts: that one is for CONTACT avatars
 * and is one letter per word ("Ada Whitlock" -> AW), which the reference also draws - but
 * it gives "Main line" -> ML and "Support" -> SU, neither of which is what the reference's
 * LINE avatars show. Two different schemes in the mockup, two functions here.
 */
export function lineInitials(name: string): string {
  const letters = name.toUpperCase().replace(/[^A-Z]/g, "");
  if (!letters) return "??";
  const first = letters[0];
  const rest = letters.slice(1);
  const consonant = rest.split("").find((ch) => !"AEIOU".includes(ch));
  return first + (consonant ?? rest[0] ?? first);
}

/** P44: pure reorder math for one drag, pulled out of the DndContext handler so it is
 * unit-testable without simulating real pointer/keyboard geometry (dnd-kit's own sensors
 * are not something jsdom can exercise reliably). Returns null for a no-op drag (dropped
 * on itself, or either id no longer in the list - e.g. it was removed mid-drag). */
export function reorderInboxes(
  inboxes: Inbox[],
  activeId: string,
  overId: string,
): Inbox[] | null {
  if (activeId === overId) return null;
  const ids = inboxes.map((inbox) => inbox.id);
  const oldIndex = ids.indexOf(activeId);
  const newIndex = ids.indexOf(overId);
  if (oldIndex === -1 || newIndex === -1) return null;
  return arrayMove(inboxes, oldIndex, newIndex);
}

/**
 * The column's own "+ New" trigger. It is a deliberate TWIN of the identically-behaved
 * NewConversationMenu inside ConversationList.tsx: the addendum keeps BOTH entry points
 * ("+ New at the top of the inbox column ... keep the header + New too"), and the two
 * differ in placement/labelling. If you change one, change the other - in particular the
 * disabled tooltip string, which is asserted in both test files.
 */
function NewConversationMenu({
  disabled,
  isLoading,
  onNew,
}: {
  disabled?: boolean;
  /** True while we don't yet know whether the user can compose (inboxes still loading) -
   * `disabled` is true in that window too, but the tooltip must stay neutral rather than
   * claiming "read-only" before that's actually known. */
  isLoading?: boolean;
  onNew: (kind: NewConversationKind) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const menuRef = React.useRef<HTMLDivElement | null>(null);

  // Same outside-click/Escape pattern as ConversationList's NewConversationMenu.
  React.useEffect(() => {
    if (!open) return undefined;

    function onMousedown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }

    function onKeydown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", onMousedown);
    document.addEventListener("keydown", onKeydown);
    return () => {
      document.removeEventListener("mousedown", onMousedown);
      document.removeEventListener("keydown", onKeydown);
    };
  }, [open]);

  // Focus the first enabled menu item when the menu opens.
  React.useEffect(() => {
    if (!open) return;
    const first = menuRef.current?.querySelector<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    );
    first?.focus();
  }, [open]);

  return (
    <div className="relative" ref={containerRef}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="New conversation"
        disabled={disabled}
        title={
          disabled && !isLoading
            ? "Read-only inbox — you can view but not start new conversations"
            : undefined
        }
        onClick={() => setOpen((value) => !value)}
        className="justify-start rounded-md bg-muted px-2.5 py-1.5 text-xs font-medium text-foreground hover:bg-foreground/10 disabled:pointer-events-none disabled:opacity-40"
      >
        <Plus className="h-3.5 w-3.5" />
        New
      </Button>

      {open && (
        <div
          role="menu"
          aria-label="New conversation"
          ref={menuRef}
          className="absolute left-0 top-9 z-20 w-44 rounded-md border border-border bg-muted p-1 shadow-lg"
        >
          <Button
            type="button"
            role="menuitem"
            variant="ghost"
            size="sm"
            onClick={() => {
              setOpen(false);
              onNew("message");
            }}
            className="w-full justify-start rounded px-2 py-1 text-xs text-foreground hover:bg-foreground/10"
          >
            <MessageSquare className="h-3.5 w-3.5" />
            New text message
          </Button>
          <Button
            type="button"
            role="menuitem"
            variant="ghost"
            size="sm"
            onClick={() => {
              setOpen(false);
              onNew("call");
            }}
            className="w-full justify-start rounded px-2 py-1 text-xs text-foreground hover:bg-foreground/10"
          >
            <Phone className="h-3.5 w-3.5" />
            New call
          </Button>
        </div>
      )}
    </div>
  );
}

/** The reference's `.nav-sec` - a section label, not a control. */
function RailSection({ children }: { children: React.ReactNode }) {
  return (
    <div className="ri-rail-label px-2 pb-1.5 pt-5 text-[0.6875rem] font-semibold text-muted-foreground">
      {children}
    </div>
  );
}

const NAV_ITEM_CLASS =
  "flex w-full items-center gap-3 rounded-[10px] px-2.5 py-2 text-left text-[0.8125rem] text-muted-foreground hover:bg-muted hover:text-foreground";

/**
 * The reference's `.brand`: the workspace mark and name. It is also the workspace
 * SWITCHER - the icon rail's switcher was the same initial in a circle, and with that rail
 * hidden on /inbox this has to be the way to a second workspace.
 */
function BrandHeader() {
  const { me, orgId, selectOrg } = useAuth();
  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);

  const org = me?.memberships.find((m) => m.org_id === orgId);
  const name = org?.org_name ?? "Workspace";

  React.useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  return (
    <div className="ri-brand relative px-1 pb-3 pt-1">
      <div className="ri-wordmark" aria-label="Ringlite"><span className="ri-signal" aria-hidden="true"><i /><i /><i /></span>ringlite<span className="ri-edition">WORKSPACE</span></div>
      <Button
        ref={triggerRef}
        type="button"
        variant="ghost"
        aria-label="Switch workspace"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-start gap-3 rounded-[10px] px-2 py-1.5 text-left hover:bg-muted"
      >
        <span
          aria-hidden="true"
          // The reference's `.brand-mark` is a GRADIENT disc, not a flat accent fill:
          // `linear-gradient(145deg, var(--accent), var(--accent-2))`. Tailwind cannot
          // express that pair, so the treatment lives in consoleTheme.css as
          // `.cx-brand-mark`, which also carries the 50% radius.
          className="cx-brand-mark grid h-[30px] w-[30px] shrink-0 place-items-center text-[0.78rem] font-bold"
        >
          {name.slice(0, 1).toUpperCase()}
        </span>
        <span className="min-w-0 flex-1 truncate text-[0.9375rem] font-semibold text-foreground">
          {name}
        </span>
      </Button>

      {open ? (
        <div
          role="menu"
          aria-label="Workspaces"
          className="absolute left-2 top-12 z-50 w-56 rounded-md border border-border bg-background p-1 shadow-lg"
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
                setOpen(false);
              }}
            >
              {m.org_name}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** One line row: the reference's `.ln` - avatar, name, number beneath, unread count. */
function LineRow({
  inbox,
  selected,
  count,
  unreadTruncated,
  canManageAccess,
  onSelect,
  onManageAccess,
  dragHandle,
}: {
  inbox: Inbox;
  selected: boolean;
  count: number;
  unreadTruncated: boolean;
  canManageAccess: boolean;
  onSelect: () => void;
  onManageAccess: () => void;
  /** P44: drag-to-reorder handle props from useSortable (attributes + listeners), or
   * undefined for a list too short to reorder (0-1 lines) - no handle renders then. */
  dragHandle?: React.HTMLAttributes<HTMLButtonElement>;
}) {
  return (
    <div className="group/line relative flex items-center gap-0.5">
      {dragHandle ? (
        <button
          type="button"
          aria-label={`Reorder ${inbox.name}`}
          title="Drag to reorder"
          className="flex h-8 w-4 shrink-0 cursor-grab touch-none items-center justify-center rounded-[var(--cx-r-xs,10px)] text-muted-foreground opacity-100 hover:bg-muted hover:text-foreground focus-visible:opacity-100 active:cursor-grabbing sm:opacity-0 sm:group-hover/line:opacity-100 sm:group-focus-within/line:opacity-100"
          {...dragHandle}
        >
          <GripVertical className="h-4 w-4" aria-hidden="true" />
        </button>
      ) : null}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        // The accessible name stays EXACTLY the line name. The row is a scope picker and
        // several suites address it by that name; the number and the count are visible
        // beside it, and the badge carries its own label.
        aria-label={inbox.name}
        aria-current={selected ? "true" : undefined}
        onClick={onSelect}
        // `h-auto` IS THE FIX, and it is not a tweak. Button's size="sm" variant is
        // `h-8 px-3 text-xs` - a FIXED 32px height, right for a one-line control and wrong
        // for this row, which stacks a 13px name over an 11.5px number. Those two line boxes
        // plus 9px of padding each side want ~52px, so the row was clamping ~50px of content
        // into 32px and the number sat jammed under the name. Nothing else was wrong: the
        // sizes, colours, radius and gap already matched the reference.
        // `h-auto` rather than a new fixed height on purpose - the row must grow if the
        // number wraps or the text scales. cn() is tailwind-merge, so this beats the
        // variant's h-8 by specificity of intent rather than by class order.
        // NOT fixed in primitives.tsx: size="sm" is correct for every other caller, and that
        // file belongs to another agent this pass.
        // py-[9px] is the reference's `.ln` padding (9px 10px) exactly; py-2 was 8px.
        // `min-w-0 flex-1` is the only addition since: the row now shares its line with the
        // access affordance below, so it must be able to shrink rather than push it off.
        className="cx-row h-auto w-full min-w-0 flex-1 items-center justify-start gap-3 rounded-[12px] px-2.5 py-[9px] text-left text-xs text-foreground"
      >
        {/* Tailwind cannot express a runtime colour - inline style is the only correct way
            to paint the per-line avatar, and `color` is the field the rail already had (it
            used to draw the same value as a 8px dot).
            The reference's avatars are GRADIENT discs, never flat fills, so the colour is
            handed to `.cx-line-avatar` as a custom property and that rule builds the
            145deg ramp off it. THE HUE IS UNCHANGED: the second stop is the same colour
            mixed toward black, so this stays user data rather than a palette choice. */}
        <span
          aria-hidden="true"
          className="cx-line-avatar grid h-7 w-7 shrink-0 place-items-center text-[0.65rem] font-semibold text-white"
          style={{ "--cx-line-av": inbox.color } as React.CSSProperties}
        >
          {lineInitials(inbox.name)}
        </span>
        {/* The reference stacks the two with a bare <br>, so the separation is entirely the
            two line boxes: `.ln-name` is 13px/1.3 and `.ln-num` is 11.5px in `--muted`.
            Reproduced literally - 0.8125rem/1.3 and 0.71875rem - rather than approximated,
            because the operator singled out "the neat spacing between the phone numbers".
            `leading-tight` (1.25) and 0.7rem (11.2px) were the old values and read tighter.
            NOTE: vitest runs with `css: false`, so nothing below is under test - no suite
            can prove a line-height. It is verified by eye against the reference only. */}
        <span className="flex min-w-0 flex-1 flex-col text-left">
          <span className="truncate text-[0.8125rem] font-semibold leading-[1.3]">
            {inbox.name}
          </span>
          <span className="cx-num truncate text-[0.71875rem] font-normal leading-[1.45] text-muted-foreground">
            {formatPhone(inbox.e164)}
          </span>
        </span>
        {count > 0 && (
          <Badge
            aria-label={`${count} unread`}
            title={
              unreadTruncated
                ? "Only the most recent unread conversations are counted"
                : undefined
            }
            className="cx-num bg-[hsl(var(--cx-live)/0.16)] text-[hsl(var(--cx-live))]"
          >
            {unreadTruncated ? `${count}+` : count}
          </Badge>
        )}
      </Button>

      {/* Access is a SIBLING of the row Button, never a child: a button may not nest
          inside a button, and the row's own click IS the scope picker - adding a second
          action inside it would make one target do two things. Splitting them into a
          flex line keeps the row's accessible name (`inbox.name`) and its single
          onSelect untouched.
          The quietness is deliberate and it is breakpoint-shaped. At `sm` and up the
          control is opacity-0 until the line is hovered or something inside it takes
          focus, so the rail still reads as the reference's clean list of numbers; it
          appears on hover, on keyboard focus, and stays up for the whole group. BELOW
          `sm` there is no hover at all - the rail is in a Sheet on a touch device - so
          `sm:opacity-0` never applies and the control is always visible. */}
      {canManageAccess ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={"Manage access to " + inbox.name + " (" + formatPhone(inbox.e164) + ")"}
          title="Manage who can use this number"
          onClick={onManageAccess}
          className="h-8 w-8 shrink-0 rounded-[var(--cx-r-xs,10px)] text-muted-foreground opacity-100 transition-opacity hover:bg-muted hover:text-foreground focus-visible:opacity-100 sm:opacity-0 sm:group-hover/line:opacity-100 sm:group-focus-within/line:opacity-100"
        >
          <Users2 className="h-4 w-4" aria-hidden="true" />
        </Button>
      ) : null}
    </div>
  );
}

/** P44: the sortable wrapper around one LineRow - the ONLY thing dnd-kit needs to know
 * about is this <li> (its ref, transform and transition), so LineRow itself stays
 * unaware it can be dragged and keeps working byte-for-byte in every non-draggable
 * caller/test. `reorderable` is false for a list of 0-1 lines - nothing to reorder, so
 * no handle, no sortable wiring, no behaviour change from before P44. */
function SortableLineRow(
  props: React.ComponentProps<typeof LineRow> & { reorderable: boolean },
) {
  const { reorderable, inbox, ...rest } = props;
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: inbox.id, disabled: !reorderable });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : undefined,
  };

  return (
    <li ref={setNodeRef} style={style}>
      <LineRow
        inbox={inbox}
        {...rest}
        dragHandle={reorderable ? { ...attributes, ...listeners } : undefined}
      />
    </li>
  );
}

export function InboxColumn({
  inboxes,
  isLoading,
  error = null,
  selection,
  onSelect,
  unread,
  unreadTruncated = false,
  onNew,
  onOpenScheduled,
  canCompose,
  canComposeLoading,
  onCollapse,
  className,
}: {
  inboxes: Inbox[];
  isLoading: boolean;
  error?: string | null;
  selection: InboxColumnSelection;
  onSelect: (selection: InboxColumnSelection) => void;
  unread: Record<string, number>;
  unreadTruncated?: boolean;
  onNew?: (kind: NewConversationKind) => void;
  /** P28: opens the send-later list. Optional, so every existing caller (and test) that
   * does not care about scheduled messages renders exactly the column it did before. */
  onOpenScheduled?: () => void;
  canCompose?: boolean;
  canComposeLoading?: boolean;
  onCollapse?: () => void;
  className?: string;
}): React.JSX.Element {
  const { me, api, logout } = useAuth();
  const { theme, toggle } = useSurfaceTheme();
  // ONE gate, shared with Sidebar - see useRailNav. The rail lists only the destinations
  // named in INBOX_RAIL_PATHS; `/inbox` is dropped because this rail IS the inbox, and
  // everything else useRailNav returns (Calls, Setup) is reached through Settings instead.
  // Filtering an already-gated list can only ever REMOVE an item, never reveal one.
  const { items, canSeeSettings, isLoading: gateLoading } = useRailNav();
  const workspaceItems = items.filter((item) => INBOX_RAIL_PATHS.includes(item.to));

  // Who may edit "manage them" is a SEPARATE gate from the nav: it is a capability on the
  // number, not a destination. `useGate` fails CLOSED while capabilities load, so for an
  // agent (and during the first paint) the rail is byte-for-byte the rail it was before -
  // no extra control, no layout shift. The backend is still the authority; this only
  // decides what to render.
  const gate = useGate();
  const canManageAccess = gate.can("inboxes:admin");

  // The drawer is MOUNTED, not just opened: an open flag would leave its three queries
  // running behind a closed panel. Holding the inbox that was acted on (null = closed)
  // means closing unmounts the drawer and the queries stop, and the next open re-seeds
  // from the server.
  const [accessInbox, setAccessInbox] = React.useState<Inbox | null>(null);

  // P44: drag-to-reorder the Lines rail. Optimistic - the cache is reordered the instant
  // the drop lands, before the PUT even resolves, the same way a reorder feels in every
  // other app; a failed save just gets silently corrected by the next natural refetch
  // (inboxesQuery's own staleTime), which is enough for a preference this low-stakes.
  const queryClient = useQueryClient();
  const reorderMutation = useMutation({
    mutationFn: (ids: string[]) => putInboxOrder(api, ids),
    onError: () => void queryClient.invalidateQueries({ queryKey: ["inboxes"] }),
  });
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over) return;
    const reordered = reorderInboxes(inboxes, String(active.id), String(over.id));
    if (!reordered) return;
    queryClient.setQueryData<Inbox[]>(["inboxes"], reordered);
    reorderMutation.mutate(reordered.map((inbox) => inbox.id));
  }

  return (
    <>
      <aside
        className={cn(
          "cx-rail flex h-full w-[240px] shrink-0 flex-col border-r border-border text-foreground",
          className,
        )}
        aria-label="Inbox column"
      >
        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-2 pt-4">
          <BrandHeader />
          {onCollapse && <button type="button" className="ri-collapse" onClick={onCollapse}><PanelLeftClose size={16} aria-hidden="true" />Collapse sidebar</button>}

          {/* Search and the bell sit above the navigation, not inside it: neither is a
              place you go. The magnifier opens the SAME command palette the icon rail
              opened (openCommandPalette), and the bell is the same component. */}
          <Button
            type="button"
            variant="ghost"
            aria-label="Search"
            title="Search (Ctrl K)"
            onClick={() => openCommandPalette()}
            className={NAV_ITEM_CLASS}
          >
            <Search className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
            Search
          </Button>

          {/* The bell is the SAME component the icon rail mounts, label and all ("Alerts",
              or "Alerts, N unread"). The word beside it is the reference's caption and is
              aria-hidden so it cannot become a second, differently-named control. */}
          <div className="flex items-center gap-2 pt-0.5 text-[0.8125rem] text-muted-foreground">
            <NotificationBell />
            <span aria-hidden="true">Notifications</span>
          </div>

          <nav aria-label="Workspace" aria-busy={gateLoading}>
            <RailSection>Workspace</RailSection>
            {workspaceItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                aria-label={item.label}
                className={({ isActive }) =>
                  cn(NAV_ITEM_CLASS, isActive && "bg-muted text-foreground")
                }
              >
                <item.icon className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
                {item.label}
              </NavLink>
            ))}

            {/* Settings is LAST in the group, after Contacts and Campaigns, because it is
                also the way to everything that used to sit in this rail: Calls, Setup and
                Trust & safety are rows in SettingsPage's own section nav now, each still
                carrying the gate it had here.

                THE ONE EXCEPTION, and it is a stranding fix rather than a second opinion
                about the design. Trust & safety is gated on is_platform_operator, which is
                independent of every settings permission, and /settings bounces a caller who
                can open no section straight back to /inbox (SettingsIndexRedirect). So a
                platform operator who is only an agent in this workspace gets no Settings row
                to reach it through - and with the icon rail hidden here, /ops would have no
                door at all. That caller, and only that caller, keeps the direct row. The two
                are mutually exclusive, so nobody ever sees both. */}
            {canSeeSettings ? (
              <NavLink
                to={SETTINGS_ITEM.to}
                aria-label={SETTINGS_ITEM.label}
                className={({ isActive }) =>
                  cn(NAV_ITEM_CLASS, isActive && "bg-muted text-foreground")
                }
              >
                <SETTINGS_ITEM.icon
                  className="h-[17px] w-[17px] shrink-0"
                  aria-hidden="true"
                />
                {SETTINGS_ITEM.label}
              </NavLink>
            ) : me?.is_platform_operator ? (
              <NavLink
                to="/ops"
                aria-label="Trust & safety"
                className={({ isActive }) =>
                  cn(NAV_ITEM_CLASS, isActive && "bg-muted text-foreground")
                }
              >
                <ShieldCheck className="h-[17px] w-[17px] shrink-0" aria-hidden="true" />
                Trust &amp; safety
              </NavLink>
            ) : null}
          </nav>

          <nav aria-label="Inboxes">
            <RailSection>Lines</RailSection>

            {isLoading ? (
              <Spinner label="Loading inboxes" />
            ) : error ? (
              <p role="alert" className="p-2 text-sm text-destructive">
                {error}
              </p>
            ) : inboxes.length === 0 ? (
              // Deliberately NOT the conversation list's "You have no inbox access yet -
              // ask an admin": that sentence teaches the next step and belongs in the one
              // place the user is actually looking. Repeating it in two columns reads as a
              // duplicated error.
              <p className="p-2 text-sm text-muted-foreground">No inboxes yet</p>
            ) : (
              <ul className="space-y-0.5">
                <li>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-current={selection.kind === "all" ? "true" : undefined}
                    onClick={() => onSelect({ kind: "all" })}
                    className="cx-row w-full justify-start rounded-[12px] px-2.5 py-2 text-left text-xs text-foreground"
                  >
                    All conversations
                  </Button>
                </li>

                <DndContext
                  sensors={sensors}
                  collisionDetection={closestCenter}
                  onDragEnd={handleDragEnd}
                >
                  <SortableContext
                    items={inboxes.map((inbox) => inbox.id)}
                    strategy={verticalListSortingStrategy}
                  >
                    {inboxes.map((inbox) => (
                      <SortableLineRow
                        key={inbox.id}
                        reorderable={inboxes.length > 1}
                        inbox={inbox}
                        selected={
                          selection.kind === "inbox" && selection.inboxId === inbox.id
                        }
                        count={unread[inbox.id] ?? 0}
                        unreadTruncated={unreadTruncated}
                        canManageAccess={canManageAccess}
                        onSelect={() => onSelect({ kind: "inbox", inboxId: inbox.id })}
                        onManageAccess={() => setAccessInbox(inbox)}
                      />
                    ))}
                  </SortableContext>
                </DndContext>

                {/* P28: a send-later message is not a conversation and has no row in the
                    conversation list, so this opens its own panel rather than setting a
                    filter that would return nothing. */}
                {onOpenScheduled && (
                  <li className="mt-1 border-t border-border pt-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={onOpenScheduled}
                      className="w-full justify-start rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-muted"
                    >
                      Scheduled
                    </Button>
                  </li>
                )}
              </ul>
            )}
          </nav>
        </div>

        {onNew && (
          <div className="flex items-center border-t border-border p-3">
            <NewConversationMenu
              disabled={!canCompose}
              isLoading={canComposeLoading}
              onNew={onNew}
            />
          </div>
        )}

        {/* "Settings icon and access appears below the phone numbers" - this cluster, sitting
            directly under the Lines group, is that access. It holds the two utility controls
            that are NOT destinations and so have nowhere in a nav to move to: the theme
            toggle and Sign out. Both are here because <Sidebar/> is hidden on /inbox; delete
            them and neither has a control on the console's busiest page.

            There is deliberately no second Settings link here. Settings is already the last
            row of the Workspace group above, and a duplicate would be two controls with the
            same accessible name in one rail - ambiguous to a screen reader and to every
            `getByRole("link", { name: "Settings" })` in the suites.

            The `console-surface` + theme class on this wrapper is load-bearing and is not
            decoration - see the identical note in Sidebar.tsx. themeToggle.css is written
            entirely against `--ex-*` tokens, and consoleTheme.light.css's palette block is
            `.console-surface.console-surface.is-light`, so a `console-surface` carrying the
            theme class only on an ANCESTOR would hand a light rail a dark-palette button. */}
        <div
          role="group"
          aria-label="Settings and account"
          className={cn(
            "console-surface",
            surfaceThemeClass(theme),
            "flex items-center gap-1 border-t border-border px-3 py-2",
          )}
        >
          <ThemeToggle theme={theme} onToggle={toggle} />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Sign out"
            onClick={logout}
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
            <span className="sr-only">Sign out</span>
          </Button>
        </div>
      </aside>

      {accessInbox ? (
        <NumberAccessDrawer
          inbox={accessInbox}
          open
          onClose={() => setAccessInbox(null)}
        />
      ) : null}
    </>
  );
}
