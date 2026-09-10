import * as React from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  Minus,
  Phone,
  Plus,
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
import { formatPhone, relativeTime } from "@/lib/format";
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

function initialsFor(title: string): string {
  const match = title.match(/[A-Za-z0-9]/g);
  if (!match || match.length === 0) return "?";
  return match.slice(0, 2).join("").toUpperCase();
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

/** One plain word per filter, shared by the Open/All menu's trigger and the collapsed
 * chip dropdown, so the same filter never reads two different ways. */
const FILTER_LABELS: Record<ConversationFilter, string> = {
  open: "Open",
  all: "All",
  unread: "Unread",
  unresponded: "Unresponded",
  important: "Important",
  snoozed: "Snoozed",
  overdue: "Overdue",
};

function FilterMenu({
  filter,
  onFilterChange,
}: {
  filter: ConversationFilter;
  onFilterChange: (filter: ConversationFilter) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement>(null);
  // A Record, not a ternary chain: P26 added two more filter values and the old chain's
  // final `else` labelled BOTH of them "Unresponded". A Record makes the compiler ask
  // for a word the next time someone adds a filter.
  const label = FILTER_LABELS[filter];

  // F19: close on outside click and Escape.
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

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-foreground hover:bg-foreground/10"
      >
        {label}
        <ChevronDown className="h-3 w-3" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Conversation filter"
          className="absolute left-0 top-8 z-20 w-28 rounded-md border border-border bg-muted p-1 shadow-lg"
        >
          <button
            role="menuitemradio"
            aria-checked={filter === "open"}
            onClick={() => {
              onFilterChange("open");
              setOpen(false);
            }}
            className="block w-full rounded px-2 py-1 text-left text-xs text-foreground hover:bg-foreground/10"
          >
            Open
          </button>
          <button
            role="menuitemradio"
            aria-checked={filter === "all"}
            onClick={() => {
              onFilterChange("all");
              setOpen(false);
            }}
            className="block w-full rounded px-2 py-1 text-left text-xs text-foreground hover:bg-foreground/10"
          >
            All
          </button>
        </div>
      )}
    </div>
  );
}

export type FilterChip = { filter: ConversationFilter; label: string; icon?: LucideIcon };

export const FILTER_CHIPS: FilterChip[] = [
  { filter: "unread", label: "Unread" },
  { filter: "important", label: "Important", icon: Star },
  { filter: "unresponded", label: "Unresponded" },
  { filter: "snoozed", label: "Snoozed" },
  { filter: "overdue", label: "Overdue" },
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
                "rounded-full px-2.5 py-1 text-xs font-medium",
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
          "rounded-full px-2.5 py-1 text-xs font-medium",
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
  const containerRef = React.useRef<HTMLDivElement>(null);

  // Same outside-click/Escape pattern as FilterMenu/ConversationHeader's "more" menu.
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

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        title={
          disabled && !isLoading
            ? "Read-only inbox — you can view but not start new conversations"
            : undefined
        }
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 rounded-md bg-muted px-2.5 py-1.5 text-xs font-medium text-foreground hover:bg-foreground/10 disabled:pointer-events-none disabled:opacity-40"
      >
        <Plus className="h-3.5 w-3.5" />
        New
      </button>
      {open && (
        <div
          role="menu"
          aria-label="New conversation"
          className="absolute right-0 top-9 z-20 w-44 rounded-md border border-border bg-muted p-1 shadow-lg"
        >
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              setOpen(false);
              onNew("message");
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-foreground/10"
          >
            <MessageSquare className="h-3.5 w-3.5" />
            New text message
          </button>
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              setOpen(false);
              onNew("call");
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-foreground/10"
          >
            <Phone className="h-3.5 w-3.5" />
            New call
          </button>
        </div>
      )}
    </div>
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
        "flex h-full min-w-0 flex-col border-r border-border bg-background",
        className,
      )}
      aria-label="Conversation list"
    >
      <div className="border-b border-border p-3">
        <div className="flex items-center gap-1">
          <div className="flex flex-1 gap-1" role="tablist" aria-label="Channel">
          <button
            role="tab"
            type="button"
            aria-selected={tab === "chats"}
            onClick={() => onTabChange("chats")}
            className={cn(
              "flex-1 rounded-md px-3 py-1.5 text-sm font-medium",
              tab === "chats"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            Chats
          </button>
          <button
            role="tab"
            type="button"
            aria-selected={tab === "calls"}
            onClick={() => onTabChange("calls")}
            className={cn(
              "flex-1 rounded-md px-3 py-1.5 text-sm font-medium",
              tab === "calls"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            Calls
          </button>
          </div>
          {onNew && (
            <NewConversationMenu
              disabled={!canCompose}
              isLoading={canComposeLoading}
              onNew={onNew}
            />
          )}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <FilterMenu filter={filter} onFilterChange={onFilterChange} />
          <FilterChips filter={filter} onFilterChange={onFilterChange} />
        </div>

        <input
          aria-label="Search conversations"
          type="search"
          placeholder="Search"
          value={q}
          onChange={(e) => onQChange(e.target.value)}
          className="mt-2 h-8 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-muted-foreground"
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
                    className={cn(
                      "flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-muted",
                      selected && "bg-muted",
                    )}
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold text-foreground">
                      {initialsFor(title)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="flex min-w-0 items-center gap-1">
                          {conversation.important && (
                            <Star
                              aria-label="Important"
                              className="h-3 w-3 shrink-0 fill-amber-400 text-amber-400"
                            />
                          )}
                          <span
                            className={cn(
                              "truncate text-sm",
                              unread
                                ? "font-bold text-foreground"
                                : "font-medium text-foreground",
                            )}
                          >
                            {title}
                          </span>
                        </span>
                        <span className="shrink-0 text-[11px] text-muted-foreground">
                          {relativeTime(conversation.last_event_at)}
                        </span>
                      </span>
                      <span className="mt-0.5 flex items-center gap-1">
                        <EventIcon conversation={conversation} />
                        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
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
