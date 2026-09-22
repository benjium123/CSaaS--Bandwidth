import * as React from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Minus,
  Phone,
  PhoneMissed,
  MessageSquare,
  Star,
  Voicemail,
  type LucideIcon,
} from "lucide-react";
import type {
  Conversation,
  ConversationFilter,
  ConversationTab,
} from "@/api/conversations";
import type { NewConversationKind } from "@/components/conversations/NewConversationPanel";
import { SlaChip } from "./SlaChip";
import { avatarHueIndex, formatPhone, initialsOf, shortRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface ConversationListProps {
  items: Conversation[];
  selectedContactE164: string | null;
  onSelect: (contactE164: string) => void;
  tab: ConversationTab;
  onTabChange: (tab: ConversationTab) => void;
  filter: ConversationFilter;
  onFilterChange: (filter: ConversationFilter) => void;
  q: string;
  onQChange: (q: string) => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  isLoading: boolean;
  onLoadMore: () => void;
  error?: string | null;
  hasNoInboxAccess?: boolean;
  className?: string;
  /** New-conversation entry point (the "+ New" button). Omitted callers (e.g. the
   * standalone unit tests below) simply get no button rendered. */
  onNew?: (kind: NewConversationKind) => void;
  /** Gates the "+ New" button the same way the Composer/Call button are gated - true
   * once we positively know the user can send from at least one number. */
  canCompose?: boolean;
  /** True while canCompose is still unknown (inboxes query in flight) - keeps the "+
   * New" button's disabled tooltip neutral instead of claiming read-only too early. */
  canComposeLoading?: boolean;
}

/** The seed a contact's avatar hue is derived from. Deliberately NOT the display name:
 * the colour is a recognition cue, so renaming someone must not repaint them. The contact
 * id when there is a contact record, their E.164 when there is not - both immutable. */
export function avatarSeedFor(conversation: Conversation): string {
  return conversation.contact?.id ?? conversation.contact_e164;
}

/** Item 36: `direction` is nullable (backend: no clear direction for this pair's latest
 * event) - render a neutral glyph instead of guessing/defaulting to an arrow either
 * direction wouldn't actually mean. */
function DirectionIcon({ direction }: { direction: "inbound" | "outbound" | null }) {
  const className = "h-3.5 w-3.5 shrink-0 text-muted-foreground";
  if (direction === "inbound") return <ArrowDownLeft className={className} />;
  if (direction === "outbound") return <ArrowUpRight className={className} />;
  return <Minus className={className} aria-label="Direction unknown" />;
}

function EventIcon({ conversation }: { conversation: Conversation }) {
  if (conversation.last_event_type === "voicemail") {
    return <Voicemail className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />;
  }
  if (conversation.last_event_type === "call" && /missed/i.test(conversation.snippet ?? "")) {
    return <PhoneMissed className="h-3.5 w-3.5 shrink-0 text-destructive" />;
  }
  return <DirectionIcon direction={conversation.direction} />;
}

/** One plain word per filter, so the same filter never reads two different ways
 * wherever it surfaces (chip, collapsed chip menu, or anywhere a caller needs to name
 * the active scope). Exported so a test can pin it against ConversationFilter: this is
 * a Record, not a ternary chain, precisely because the old chain's final `else`
 * silently labelled two different filters "Unresponded". */
export const FILTER_LABELS: Record<ConversationFilter, string> = {
  open: "Open",
  all: "All",
  unread: "Unread",
  unresponded: "Unresponded",
  important: "Important",
  snoozed: "Snoozed",
  overdue: "Overdue",
};

export type FilterChip = { filter: ConversationFilter; label: string; icon?: LucideIcon };

/**
 * Three, in the reference's order: All, Unresponded, Important.
 * (docs/design/console-reference.html - "four reduced to three".)
 *
 * "All" is the widest scope the backend offers (filter=all: closed and snoozed
 * conversations included), which is what keeps a snoozed conversation reachable now
 * that the Snoozed chip and the Snoozed rail row are gone - "open", the resting
 * filter, deliberately hides anything snoozed into the future.
 *
 * Unread / Snoozed / Overdue are no longer offered AS FILTERS. The underlying state is
 * still rendered: the unread dot and bold name on a row, the SLA/Overdue chip on a row,
 * and the snooze control in ConversationHeader. Every one of those filter values is
 * still accepted by the backend and by ConversationFilter, so restoring a chip is a
 * one-line change.
 */
export const FILTER_CHIPS: FilterChip[] = [
  { filter: "all", label: FILTER_LABELS.all },
  { filter: "unresponded", label: FILTER_LABELS.unresponded },
  { filter: "important", label: FILTER_LABELS.important },
];

export const MAX_VISIBLE_CHIPS = 4;

export function FilterChips({
  chips = FILTER_CHIPS,
  filter,
  onFilterChange,
}: {
  chips?: FilterChip[];
  filter: ConversationFilter;
  onFilterChange: (filter: ConversationFilter) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return undefined;
    function onPointerDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (chips.length <= MAX_VISIBLE_CHIPS) {
    return (
      <>
        {chips.map((chip) => {
          const Icon = chip.icon;
          const active = chip.filter === filter;
          return (
            <button
              key={chip.filter}
              type="button"
              aria-pressed={active}
              onClick={() => onFilterChange(active ? "open" : chip.filter)}
              className={cn(
                "cx-chip rounded-full px-2.5 py-1 text-xs font-medium",
                active
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-foreground hover:bg-foreground/10",
                Icon && "flex items-center gap-1",
              )}
            >
              {Icon ? <Icon className="h-3 w-3" /> : null}
              {chip.label}
            </button>
          );
        })}
      </>
    );
  }

  // More than MAX_VISIBLE_CHIPS things in a row stops being a row of choices and
  // becomes noise - collapse to a single menu so the choices stay legible.
  const activeChip = chips.find((chip) => chip.filter === filter);
  const triggerLabel = activeChip ? `Filter: ${activeChip.label}` : "Filter";

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "cx-chip rounded-full px-2.5 py-1 text-xs font-medium",
          activeChip
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground hover:bg-foreground/10",
        )}
      >
        {triggerLabel}
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Filter conversations"
          className="absolute left-0 top-8 z-20 w-28 rounded-md border border-border bg-muted p-1 shadow-lg"
        >
          {chips.map((chip) => {
            const checked = chip.filter === filter;
            return (
              <button
                key={chip.filter}
                type="button"
                role="menuitemradio"
                aria-checked={checked}
                onClick={() => {
                  onFilterChange(checked ? "open" : chip.filter);
                  setOpen(false);
                }}
                className="block w-full rounded px-2 py-1 text-left text-xs text-foreground hover:bg-foreground/10"
              >
                {chip.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * The reference's two `.icon-btn`s at the right of the list header: a phone and a speech
 * bubble, one tap each to START something.
 *
 * This REPLACES the old "+ New" dropdown and loses no capability - both destinations
 * ("New call", "New text message") are still reachable, and are now one click rather than
 * two. The gate and its tooltip are carried over unchanged: disabled when the user cannot
 * compose, and silent (no title) while that is still unknown, so a read-only claim is
 * never made before the inboxes query has answered.
 */
function NewConversationButtons({
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
  const title =
    disabled && !isLoading
      ? "Read-only inbox — you can view but not start new conversations"
      : undefined;

  return (
    <span className="flex items-center gap-1">
      <button
        type="button"
        aria-label="New call"
        disabled={disabled}
        title={title}
        onClick={() => onNew("call")}
        className="cx-icon-btn grid h-8 w-8 place-items-center disabled:pointer-events-none disabled:opacity-40"
      >
        <Phone className="h-4 w-4" aria-hidden="true" />
      </button>
      <button
        type="button"
        aria-label="New text message"
        disabled={disabled}
        title={title}
        onClick={() => onNew("message")}
        className="cx-icon-btn grid h-8 w-8 place-items-center disabled:pointer-events-none disabled:opacity-40"
      >
        <MessageSquare className="h-4 w-4" aria-hidden="true" />
      </button>
    </span>
  );
}

export function ConversationList({
  items,
  selectedContactE164,
  onSelect,
  tab,
  onTabChange,
  filter,
  onFilterChange,
  q,
  onQChange,
  hasNextPage,
  isFetchingNextPage,
  isLoading,
  onLoadMore,
  error,
  hasNoInboxAccess,
  className,
  onNew,
  canCompose,
  canComposeLoading,
}: ConversationListProps) {
  return (
    <aside
      className={cn(
        "ri-conversations flex h-full min-w-0 flex-col border-r border-border bg-background",
        className,
      )}
      aria-label="Conversation list"
    >
      <div className="ri-list-heading border-b border-border p-3">
        <p className="ri-kicker">Conversations</p>
        {/* The reference's `.list-top`: the two tabs sit at the LEFT at their own width
            (they are a choice between two things, not a segmented control filling the
            column) and the two start-something icon buttons sit at the right. */}
        <div className="flex items-center gap-1">
          <div className="flex flex-1 items-center gap-4" role="tablist" aria-label="Channel">
          <button
            role="tab"
            type="button"
            aria-selected={tab === "chats"}
            onClick={() => onTabChange("chats")}
            className="cx-tab px-1 py-1.5 text-[0.9375rem] font-semibold"
          >
            Chats
          </button>
          <button
            role="tab"
            type="button"
            aria-selected={tab === "calls"}
            onClick={() => onTabChange("calls")}
            className="cx-tab px-1 py-1.5 text-[0.9375rem] font-semibold"
          >
            Calls
          </button>
          </div>
          {onNew && (
            <NewConversationButtons
              disabled={!canCompose}
              isLoading={canComposeLoading}
              onNew={onNew}
            />
          )}
        </div>

        {/* Three pills and nothing else. The resting state is `open` with no pill
            pressed; pressing the active pill again returns to it. */}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <FilterChips filter={filter} onFilterChange={onFilterChange} />
        </div>

        {/* KEPT, though the reference draws no search box in this column. The rail's
            magnifier is the app-wide command palette (Sidebar.tsx, Ctrl-K) and searches
            navigation, not this list; this field is the ONLY way to reach the backend's
            `q=` conversation search, which looks inside message bodies and contact names.
            Deleting it to match the picture would delete the capability, which is the one
            thing the reference is explicit it does not do - it removes duplicate
            navigation, not features. */}
        <input
          aria-label="Search conversations"
          type="search"
          placeholder="Search"
          value={q}
          onChange={(e) => onQChange(e.target.value)}
          className="cx-input mt-2 h-8 w-full px-2 text-xs placeholder:text-muted-foreground"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {hasNoInboxAccess ? (
          <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
            You have no inbox access yet — ask an admin
          </div>
        ) : error ? (
          <p role="alert" className="p-4 text-sm text-destructive">
            {error}
          </p>
        ) : isLoading ? (
          <p className="p-4 text-sm text-muted-foreground">Loading conversations…</p>
        ) : items.length === 0 ? (
          <p className="p-4 text-sm text-muted-foreground">No conversations yet</p>
        ) : (
          <ul className="divide-y divide-border" aria-label="Conversations">
            {items.map((conversation) => {
              const title =
                conversation.contact?.display_name ??
                formatPhone(conversation.contact_e164);
              const selected = conversation.contact_e164 === selectedContactE164;
              const unread = conversation.unread;
              return (
                // Item 11/12: thread_id is null for a call-only pair with no message
                // thread yet, so it can't key the list (two such pairs would collide) -
                // the (our_e164, contact_e164) pair is always unique per row instead.
                <li key={`${conversation.our_e164}|${conversation.contact_e164}`}>
                  <button
                    type="button"
                    onClick={() => onSelect(conversation.contact_e164)}
                    aria-current={selected ? "true" : undefined}
                    className="cx-row flex w-full items-center gap-2.5 px-3 py-2 text-left"
                  >
                    {/* data-hue is the ONLY thing choosing the colour; the seven hues
                        themselves live in consoleTheme.css. See avatarHueIndex. */}
                    <span
                      data-hue={avatarHueIndex(avatarSeedFor(conversation))}
                      className="cx-avatar flex h-9 w-9 shrink-0 items-center justify-center rounded-full font-semibold"
                    >
                      {initialsOf(title)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="flex min-w-0 items-center gap-1.5">
                          {/* Unread is carried by the dot AND the weight/colour of the
                              name, never by colour alone. */}
                          {unread && <span className="cx-unread-dot" aria-hidden="true" />}
                          {conversation.important && (
                            <Star
                              aria-label="Important"
                              className="h-3 w-3 shrink-0 fill-[hsl(var(--cx-flag))] text-[hsl(var(--cx-flag))]"
                            />
                          )}
                          <span
                            className={cn(
                              "truncate text-[0.8125rem]",
                              unread
                                ? "font-semibold text-foreground"
                                : "font-medium text-foreground/85",
                            )}
                          >
                            {title}
                          </span>
                        </span>
                        <span className="cx-num shrink-0 text-[0.625rem] text-muted-foreground">
                          {shortRelativeTime(conversation.last_event_at)}
                        </span>
                      </span>
                      <span className="mt-0.5 flex items-center gap-1">
                        <EventIcon conversation={conversation} />
                        <span
                          className={cn(
                            "min-w-0 flex-1 truncate text-xs",
                            unread ? "text-foreground" : "text-muted-foreground",
                          )}
                        >
                          {/* The reference's `.row-prev b`: our own last word is owned out
                              loud, so a row whose last event was OURS is visibly not
                              waiting on a reply from us. */}
                          {conversation.direction === "outbound" && (
                            <span className="font-semibold text-foreground">You: </span>
                          )}
                          {conversation.snippet || "No messages"}
                        </span>
                        <SlaChip sla={conversation.sla} className="shrink-0" />
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {hasNextPage && (
        <div className="border-t border-border p-2">
          <button
            type="button"
            onClick={onLoadMore}
            disabled={isFetchingNextPage}
            className="w-full rounded-md px-3 py-1.5 text-xs font-medium text-foreground hover:bg-muted disabled:opacity-50"
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </aside>
  );
}
