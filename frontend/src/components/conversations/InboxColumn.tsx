import * as React from "react";
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
  ChevronDown,
  GripVertical,
  PanelLeftClose,
  MessageSquare,
  Phone,
  Plus,
  Users2,
} from "lucide-react";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { putInboxOrder, type Inbox } from "@/api/conversations";
import type { NewConversationKind } from "@/components/conversations/NewConversationPanel";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
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

const FOLDS_STORAGE_KEY = "ringlite.inbox.folds";
/** Lines the sidebar shows before folding the rest behind "+N more" (fits ~720px). */
const MAX_VISIBLE_LINES = 14;

export type InboxGroup = { id: string; name: string; inboxes: Inbox[] };

/** Department folds for the Lines list, in the inboxes' own display order. Returns null
 * when no line belongs to any department, so the caller keeps the flat list. */
export function groupInboxesByDepartment(inboxes: Inbox[]): InboxGroup[] | null {
  if (!inboxes.some((inbox) => (inbox.departments ?? []).length > 0)) return null;
  const byId = new Map<string, InboxGroup>();
  const other: InboxGroup = { id: "__other", name: "Other lines", inboxes: [] };
  for (const inbox of inboxes) {
    const dept = [...(inbox.departments ?? [])].sort((a, b) => a.name.localeCompare(b.name))[0];
    if (!dept) {
      other.inboxes.push(inbox);
      continue;
    }
    let group = byId.get(dept.id);
    if (!group) {
      group = { id: dept.id, name: dept.name, inboxes: [] };
      byId.set(dept.id, group);
    }
    group.inboxes.push(inbox);
  }
  const groups = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  return other.inboxes.length ? [...groups, other] : groups;
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
  needsYou,
  dock,
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
  /** Phase 1b: "needs you" chips under New message, and the phone dock at the bottom. */
  needsYou?: React.ReactNode;
  dock?: React.ReactNode;
  className?: string;
}): React.JSX.Element {
  const { api } = useAuth();

  // Who may edit "manage them" is a SEPARATE gate from the nav: it is a capability on the
  // number, not a destination. `useGate` fails CLOSED while capabilities load, so for an
  // agent (and during the first paint) the rail is byte-for-byte the rail it was before -
  // no extra control, no layout shift. The backend is still the authority; this only
  // decides what to render.
  const gate = useGate();
  const canManageAccess = gate.can("inboxes:admin");

  // Phase 1b: lines grouped into department folds (a line granted to several departments
  // is listed under the first, alphabetically). No departments at all -> the flat,
  // drag-to-reorder list exactly as before. Fold state is remembered per browser.
  const groups = React.useMemo(() => groupInboxesByDepartment(inboxes), [inboxes]);
  const [closedFolds, setClosedFolds] = React.useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(localStorage.getItem(FOLDS_STORAGE_KEY) ?? "{}") as Record<string, boolean>;
    } catch {
      return {};
    }
  });
  function toggleFold(id: string) {
    setClosedFolds((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      try {
        localStorage.setItem(FOLDS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable: the fold still toggles for this session */
      }
      return next;
    });
  }
  const visibleLineCount = groups
    ? groups.reduce((n, g) => n + (closedFolds[g.id] ? 0 : g.inboxes.length), 0)
    : inboxes.length;

  // Overflow rule (spec §2): past MAX_VISIBLE_LINES even compact rows would scroll, so each
  // open fold keeps the lines that need you (unread, or the one you are on) plus the first
  // few others, and the rest sit behind "+N more" until that fold is expanded.
  const [expandedFolds, setExpandedFolds] = React.useState<Record<string, boolean>>({});
  const openFoldCount = groups ? groups.filter((g) => !closedFolds[g.id]).length : 0;
  const overflowing = visibleLineCount > MAX_VISIBLE_LINES;
  function linesToShow(group: InboxGroup): Inbox[] {
    if (!overflowing || expandedFolds[group.id]) return group.inboxes;
    const limit = Math.max(3, Math.floor(MAX_VISIBLE_LINES / Math.max(1, openFoldCount)));
    const keep = new Set(
      group.inboxes
        .filter(
          (inbox) =>
            (unread[inbox.id] ?? 0) > 0 ||
            (selection.kind === "inbox" && selection.inboxId === inbox.id),
        )
        .map((inbox) => inbox.id),
    );
    for (const inbox of group.inboxes) {
      if (keep.size >= limit) break;
      keep.add(inbox.id);
    }
    return group.inboxes.filter((inbox) => keep.has(inbox.id));
  }

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
          {/* Phase 1 (INBOX_NAV_SPEC.md): brand, workspace switcher, search, bell and the
              Workspace links live in the top bar now. This column is only the inbox. */}
          <div className="flex items-center gap-2 pb-2">
            {onNew && (
              <div className="min-w-0 flex-1">
                <NewConversationMenu disabled={!canCompose} isLoading={canComposeLoading} onNew={onNew} />
              </div>
            )}
            {onCollapse && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Collapse sidebar"
                title="Collapse sidebar"
                onClick={onCollapse}
                className="ml-auto h-8 w-8 shrink-0"
              >
                <PanelLeftClose className="h-4 w-4" aria-hidden="true" />
              </Button>
            )}
          </div>
          {needsYou}

          <nav aria-label="Inboxes" className={cn(visibleLineCount > 6 && "ri-lines-compact")}>
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

                {groups ? (
                  groups.map((group) => {
                    const open = !closedFolds[group.id];
                    const groupUnread = group.inboxes.reduce((n, inbox) => n + (unread[inbox.id] ?? 0), 0);
                    return (
                      <li key={group.id}>
                        <button
                          type="button"
                          aria-expanded={open}
                          onClick={() => toggleFold(group.id)}
                          className="flex w-full items-center gap-1.5 rounded-md px-2 pb-1 pt-2 text-left text-[0.75rem] font-semibold text-muted-foreground hover:bg-muted hover:text-foreground"
                        >
                          <ChevronDown
                            className={cn("h-3.5 w-3.5 shrink-0 transition-transform", !open && "-rotate-90")}
                            aria-hidden="true"
                          />
                          <span className="truncate">{group.name}</span>
                          <span className="font-normal text-muted-foreground/70">{group.inboxes.length}</span>
                          {!open && groupUnread > 0 && (
                            <span
                              aria-label={`${groupUnread} unread in ${group.name}`}
                              className="ml-auto rounded-full bg-primary px-1.5 text-[0.65rem] font-semibold text-primary-foreground"
                            >
                              {groupUnread}
                            </span>
                          )}
                        </button>
                        {open && (
                          <ul className="space-y-0.5">
                            {linesToShow(group).map((inbox) => (
                              <li key={inbox.id}>
                                <LineRow
                                  inbox={inbox}
                                  selected={selection.kind === "inbox" && selection.inboxId === inbox.id}
                                  count={unread[inbox.id] ?? 0}
                                  unreadTruncated={unreadTruncated}
                                  canManageAccess={canManageAccess}
                                  onSelect={() => onSelect({ kind: "inbox", inboxId: inbox.id })}
                                  onManageAccess={() => setAccessInbox(inbox)}
                                />
                              </li>
                            ))}
                            {linesToShow(group).length < group.inboxes.length && (
                              <li>
                                <button
                                  type="button"
                                  aria-label={`Show ${group.inboxes.length - linesToShow(group).length} more in ${group.name}`}
                                  onClick={() => setExpandedFolds((prev) => ({ ...prev, [group.id]: true }))}
                                  className="w-full rounded-md px-2.5 py-1 text-left text-[0.75rem] text-muted-foreground hover:bg-muted hover:text-foreground"
                                >
                                  +{group.inboxes.length - linesToShow(group).length} more
                                </button>
                              </li>
                            )}
                          </ul>
                        )}
                      </li>
                    );
                  })
                ) : (
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
                )}

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

        {dock}
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
