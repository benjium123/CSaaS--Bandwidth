import * as React from "react";
import { MessageSquare, Phone, Plus } from "lucide-react";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import type { Inbox } from "@/api/conversations";
import type { NewConversationKind } from "@/components/conversations/NewConversationPanel";
import { cn } from "@/lib/utils";

export type InboxColumnSelection =
  | { kind: "all" }
  | { kind: "inbox"; inboxId: string }
  | { kind: "view"; view: "important" | "unresponded" | "snoozed" | "overdue" };

type ViewKey = "important" | "unresponded" | "snoozed" | "overdue";

/**
 * Kept as a local component so the four view rows cannot drift apart.
 * The markup/classes are the same as the old Important row.
 */
function ViewRow({
  view,
  label,
  selection,
  onSelect,
}: {
  view: ViewKey;
  label: string;
  selection: InboxColumnSelection;
  onSelect: (selection: InboxColumnSelection) => void;
}) {
  const selected = selection.kind === "view" && selection.view === view;

  return (
    <li>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-current={selected ? "true" : undefined}
        onClick={() => onSelect({ kind: "view", view })}
        className={cn(
          "w-full justify-start rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-muted",
          selected && "bg-muted",
        )}
      >
        {label}
      </Button>
    </li>
  );
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

export function InboxColumn({
  inboxes,
  isLoading,
  error = null,
  selection,
  onSelect,
  unread,
  unreadTruncated = false,
  onNew,
  canCompose,
  canComposeLoading,
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
  canCompose?: boolean;
  canComposeLoading?: boolean;
  className?: string;
}): React.JSX.Element {
  return (
    <aside
      className={cn(
        "flex h-full w-[220px] shrink-0 flex-col border-r border-border bg-background text-foreground",
        className,
      )}
      aria-label="Inbox column"
    >
      {onNew && (
        <div className="flex items-center border-b border-border p-3">
          <NewConversationMenu
            disabled={!canCompose}
            isLoading={canComposeLoading}
            onNew={onNew}
          />
        </div>
      )}

      <nav aria-label="Inboxes" className="min-h-0 flex-1 overflow-y-auto p-2">
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
          <ul className="space-y-1">
            <li>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-current={selection.kind === "all" ? "true" : undefined}
                onClick={() => onSelect({ kind: "all" })}
                className={cn(
                  "w-full justify-start rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-muted",
                  selection.kind === "all" && "bg-muted",
                )}
              >
                All conversations
              </Button>
            </li>

            {inboxes.map((inbox) => {
              const count = unread[inbox.id] ?? 0;
              const selected =
                selection.kind === "inbox" && selection.inboxId === inbox.id;

              return (
                <li key={inbox.id}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-current={selected ? "true" : undefined}
                    onClick={() => onSelect({ kind: "inbox", inboxId: inbox.id })}
                    className={cn(
                      "w-full justify-start rounded px-2 py-1.5 text-left text-xs text-foreground hover:bg-muted",
                      selected && "bg-muted",
                    )}
                  >
                    {/* Tailwind cannot express a runtime colour - inline style is the
                        only correct way to draw the per-inbox dot. */}
                    <span
                      aria-hidden="true"
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: inbox.color }}
                    />
                    <span className="min-w-0 flex-1 truncate text-left">{inbox.name}</span>
                    {count > 0 && (
                      <Badge
                        aria-label={`${count} unread`}
                        title={
                          unreadTruncated
                            ? "Only the most recent unread conversations are counted"
                            : undefined
                        }
                        className="bg-muted text-foreground"
                      >
                        {unreadTruncated ? `${count}+` : count}
                      </Badge>
                    )}
                  </Button>
                </li>
              );
            })}

            <li aria-hidden="true" className="my-1 border-t border-border" />

            <ViewRow
              view="important"
              label="Important"
              selection={selection}
              onSelect={onSelect}
            />
            <ViewRow
              view="unresponded"
              label="Unresponded"
              selection={selection}
              onSelect={onSelect}
            />
            <ViewRow
              view="snoozed"
              label="Snoozed"
              selection={selection}
              onSelect={onSelect}
            />
            <ViewRow
              view="overdue"
              label="Overdue"
              selection={selection}
              onSelect={onSelect}
            />
          </ul>
        )}
      </nav>
    </aside>
  );
}
