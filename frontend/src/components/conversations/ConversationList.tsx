import * as React from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  PhoneMissed,
  Voicemail,
} from "lucide-react";
import type {
  Conversation,
  ConversationFilter,
  ConversationTab,
} from "@/api/conversations";
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
}

function initialsFor(title: string): string {
  const match = title.match(/[A-Za-z0-9]/g);
  if (!match || match.length === 0) return "?";
  return match.slice(0, 2).join("").toUpperCase();
}

function EventIcon({ conversation }: { conversation: Conversation }) {
  const className = "h-3.5 w-3.5 shrink-0 text-neutral-400";
  if (conversation.last_event_type === "voicemail") {
    return <Voicemail className={className} />;
  }
  if (conversation.last_event_type === "call") {
    if (/missed/i.test(conversation.snippet ?? "")) {
      return <PhoneMissed className="h-3.5 w-3.5 shrink-0 text-red-400" />;
    }
    return conversation.direction === "inbound" ? (
      <ArrowDownLeft className={className} />
    ) : (
      <ArrowUpRight className={className} />
    );
  }
  return conversation.direction === "inbound" ? (
    <ArrowDownLeft className={className} />
  ) : (
    <ArrowUpRight className={className} />
  );
}

function FilterMenu({
  filter,
  onFilterChange,
}: {
  filter: ConversationFilter;
  onFilterChange: (filter: ConversationFilter) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement>(null);
  const label = filter === "all" ? "All" : filter === "open" ? "Open" : filter === "unread" ? "Unread" : "Unresponded";

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
        className="flex items-center gap-1 rounded-full bg-neutral-800 px-2.5 py-1 text-xs font-medium text-neutral-200 hover:bg-neutral-700"
      >
        {label}
        <ChevronDown className="h-3 w-3" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Conversation filter"
          className="absolute left-0 top-8 z-20 w-28 rounded-md border border-neutral-700 bg-neutral-800 p-1 shadow-lg"
        >
          <button
            role="menuitemradio"
            aria-checked={filter === "open"}
            onClick={() => {
              onFilterChange("open");
              setOpen(false);
            }}
            className="block w-full rounded px-2 py-1 text-left text-xs text-neutral-200 hover:bg-neutral-700"
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
            className="block w-full rounded px-2 py-1 text-left text-xs text-neutral-200 hover:bg-neutral-700"
          >
            All
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
}: ConversationListProps) {
  return (
    <aside
      className={cn(
        "flex h-full min-w-0 flex-col border-r border-neutral-800 bg-neutral-900",
        className,
      )}
      aria-label="Conversation list"
    >
      <div className="border-b border-neutral-800 p-3">
        <div className="flex gap-1" role="tablist" aria-label="Channel">
          <button
            role="tab"
            type="button"
            aria-selected={tab === "chats"}
            onClick={() => onTabChange("chats")}
            className={cn(
              "flex-1 rounded-md px-3 py-1.5 text-sm font-medium",
              tab === "chats"
                ? "bg-neutral-800 text-neutral-50"
                : "text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200",
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
                ? "bg-neutral-800 text-neutral-50"
                : "text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200",
            )}
          >
            Calls
          </button>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <FilterMenu filter={filter} onFilterChange={onFilterChange} />
          <button
            type="button"
            aria-pressed={filter === "unread"}
            onClick={() =>
              onFilterChange(filter === "unread" ? "open" : "unread")
            }
            className={cn(
              "rounded-full px-2.5 py-1 text-xs font-medium",
              filter === "unread"
                ? "bg-neutral-100 text-neutral-900"
                : "bg-neutral-800 text-neutral-200 hover:bg-neutral-700",
            )}
          >
            Unread
          </button>
          <button
            type="button"
            aria-pressed={filter === "unresponded"}
            onClick={() =>
              onFilterChange(filter === "unresponded" ? "open" : "unresponded")
            }
            className={cn(
              "rounded-full px-2.5 py-1 text-xs font-medium",
              filter === "unresponded"
                ? "bg-neutral-100 text-neutral-900"
                : "bg-neutral-800 text-neutral-200 hover:bg-neutral-700",
            )}
          >
            Unresponded
          </button>
        </div>

        <input
          aria-label="Search conversations"
          type="search"
          placeholder="Search"
          value={q}
          onChange={(e) => onQChange(e.target.value)}
          className="mt-2 h-8 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {hasNoInboxAccess ? (
          <div className="flex h-full items-center justify-center p-4 text-center text-sm text-neutral-400">
            You have no inbox access yet — ask an admin
          </div>
        ) : error ? (
          <p role="alert" className="p-4 text-sm text-red-400">
            {error}
          </p>
        ) : isLoading ? (
          <p className="p-4 text-sm text-neutral-400">Loading conversations…</p>
        ) : items.length === 0 ? (
          <p className="p-4 text-sm text-neutral-400">No conversations yet</p>
        ) : (
          <ul className="divide-y divide-neutral-800" aria-label="Conversations">
            {items.map((conversation) => {
              const title =
                conversation.contact?.display_name ??
                formatPhone(conversation.contact_e164);
              const selected = conversation.contact_e164 === selectedContactE164;
              const unread = conversation.unread > 0;
              return (
                <li key={conversation.thread_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(conversation.contact_e164)}
                    aria-current={selected ? "true" : undefined}
                    className={cn(
                      "flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-neutral-800",
                      selected && "bg-neutral-800",
                    )}
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-neutral-700 text-xs font-semibold text-neutral-100">
                      {initialsFor(title)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span
                          className={cn(
                            "truncate text-sm",
                            unread
                              ? "font-bold text-neutral-50"
                              : "font-medium text-neutral-200",
                          )}
                        >
                          {title}
                        </span>
                        <span className="shrink-0 text-[11px] text-neutral-500">
                          {relativeTime(conversation.last_event_at)}
                        </span>
                      </span>
                      <span className="mt-0.5 flex items-center gap-1">
                        <EventIcon conversation={conversation} />
                        <span className="min-w-0 flex-1 truncate text-xs text-neutral-500">
                          {conversation.snippet || "No messages"}
                        </span>
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
        <div className="border-t border-neutral-800 p-2">
          <button
            type="button"
            onClick={onLoadMore}
            disabled={isFetchingNextPage}
            className="w-full rounded-md px-3 py-1.5 text-xs font-medium text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </aside>
  );
}
