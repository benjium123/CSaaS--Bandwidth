import * as React from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import type { ContactOut } from "@/api/contacts";
import { fetchConversations, fetchInboxes } from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { EmptyState, Input, Kbd, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

type PaletteResult = {
  id: string;
  group: "Inboxes" | "Contacts" | "Conversations";
  title: string;
  subtitle?: string;
  to: string;
};

// Tiny module-level pub/sub so any component can open the palette without prop
// drilling or a context provider. The Sidebar uses this, while Sidebar.test.tsx
// can still render <Sidebar /> with no props and no provider.
const listeners = new Set<() => void>();

export function openCommandPalette(): void {
  listeners.forEach((listener) => listener());
}

function useDebouncedValue(value: string, delay: number): string {
  const [debounced, setDebounced] = React.useState(value);

  React.useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);

  return debounced;
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const { api } = useAuth();
  const [query, setQuery] = React.useState("");
  const debounced = useDebouncedValue(query, 200);
  const term = debounced.trim();
  const enabled = term.length >= 2;

  // IMPORTANT: these query keys start with the literal "command-palette" segment.
  // Elsewhere the app calls:
  //   queryClient.setQueriesData({ queryKey: ["conversations"] }, d => d.pages.map(...))
  // and
  //   invalidateQueries({ queryKey: ["contacts"] })
  // TanStack Query matches keys BY PREFIX. If a palette query were filed under
  // ["conversations"] or ["contacts"], the broad updater/invalidator would hit it
  // and the setQueriesData updater would throw on our non-paginated shape.
  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });

  const contactsQuery = useQuery({
    queryKey: ["command-palette", "contacts", term],
    enabled,
    queryFn: () =>
      api.request<ContactOut[]>(`/api/v1/contacts?q=${encodeURIComponent(term)}`),
  });

  const conversationsQuery = useQuery({
    queryKey: ["command-palette", "conversations", term],
    enabled,
    queryFn: () => fetchConversations(api, { q: term, filter: "all" }),
  });

  const results = React.useMemo<PaletteResult[]>(() => {
    const next: PaletteResult[] = [];
    // Below the 2-character threshold NOTHING is a result. Inboxes are matched locally,
    // so without this guard `"".includes()` would match every inbox and the combobox
    // would advertise aria-expanded / aria-activedescendant for options that the
    // "type at least 2 characters" branch never renders.
    if (!enabled) return next;

    const needle = term.toLowerCase();
    const inboxes = inboxesQuery.data ?? [];
    // FILTER FIRST, then cap: capping first would hide a match that happens to be the
    // sixth inbox in the list.
    for (const inbox of inboxes
      .filter((inbox) => inbox.name.toLowerCase().includes(needle))
      .slice(0, 5)) {
      next.push({
        id: `inbox-${inbox.id}`,
        group: "Inboxes",
        title: inbox.name,
        to: `/inbox/${inbox.id}`,
      });
    }

    const contacts = contactsQuery.data ?? [];
    for (const contact of contacts.slice(0, 5)) {
      const primary = contact.phones.find((phone) => phone.is_primary) ?? contact.phones[0];
      next.push({
        id: `contact-${contact.id}`,
        group: "Contacts",
        title: contact.display_name,
        subtitle: primary ? formatPhone(primary.e164) : undefined,
        to: `/contacts/${contact.id}`,
      });
    }

    const conversations = conversationsQuery.data?.items ?? [];
    for (const item of conversations.slice(0, 5)) {
      // Do NOT route a conversation to `/inbox/:inboxId/:threadId`. `thread_id`
      // is nullable (a call-only pair has none) and ConversationsPage deliberately
      // ignores the `:threadId` route param — `?contact=` / `?our=` is the real
      // thread selector.
      next.push({
        id: `conversation-${item.inbox_id}-${item.contact_e164}-${item.our_e164}`,
        group: "Conversations",
        title: item.contact?.display_name ?? formatPhone(item.contact_e164),
        subtitle: item.snippet ?? "",
        to: `/inbox/${item.inbox_id}?contact=${encodeURIComponent(
          item.contact_e164,
        )}&our=${encodeURIComponent(item.our_e164)}`,
      });
    }

    return next;
  }, [enabled, inboxesQuery.data, contactsQuery.data, conversationsQuery.data, term]);

  const [activeIndex, setActiveIndex] = React.useState(0);
  React.useEffect(() => {
    setActiveIndex(0);
  }, [results]);

  const optionRefs = React.useRef<Record<string, HTMLDivElement | null>>({});
  React.useEffect(() => {
    const active = results[activeIndex];
    if (!active) return;
    const el = optionRefs.current[active.id];
    el?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, results]);

  const inputRef = React.useRef<HTMLInputElement | null>(null);
  React.useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const activeId =
    results.length > 0 ? `command-palette-option-${results[activeIndex].id}` : undefined;

  function handleInputKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }

    if (event.key === "Tab") {
      // The input is the ONLY focusable element in this dialog by design; letting
      // Tab through would move focus to the browser chrome or body, so keep it put.
      event.preventDefault();
      return;
    }

    if (results.length === 0) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % results.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + results.length) % results.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(results.length - 1);
    } else if (event.key === "Enter") {
      const selected = results[activeIndex];
      if (selected) {
        event.preventDefault();
        navigate(selected.to);
        onClose();
      }
    }
  }

  const groups = React.useMemo(() => {
    const groupNames = ["Inboxes", "Contacts", "Conversations"] as const;
    return groupNames
      .map((groupName) => ({
        groupName,
        items: results.filter((result) => result.group === groupName),
      }))
      .filter((group) => group.items.length > 0);
  }, [results]);

  const isSearchLoading =
    enabled && (contactsQuery.isLoading || conversationsQuery.isLoading);

  return (
    <div className="dark fixed inset-0 z-50 bg-black/50" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        className="mx-auto mt-20 w-full max-w-lg rounded-lg border border-border bg-background shadow-lg"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="border-b border-border p-3">
          <Input
            ref={inputRef}
            role="combobox"
            aria-expanded={results.length > 0}
            aria-controls="command-palette-listbox"
            aria-autocomplete="list"
            aria-activedescendant={activeId}
            aria-label="Search"
            placeholder="Search contacts, inboxes and conversations"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={handleInputKeyDown}
          />
        </div>

        <div className="max-h-80 overflow-y-auto p-2">
          {term.length < 2 ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              Type at least 2 characters.
            </p>
          ) : isSearchLoading ? (
            <Spinner label="Searching" />
          ) : (
            <>
              {results.length === 0 ? (
                <EmptyState
                  title="No matches"
                  description="Try a name, a number or an inbox name."
                />
              ) : (
                <div
                  id="command-palette-listbox"
                  role="listbox"
                  aria-label="Search results"
                >
                  {groups.map(({ groupName, items }) => (
                    <div key={groupName} role="group" aria-label={groupName}>
                      {items.map((result) => {
                        const globalIndex = results.indexOf(result);
                        return (
                          <div
                            key={result.id}
                            ref={(node) => {
                              optionRefs.current[result.id] = node;
                            }}
                            id={`command-palette-option-${result.id}`}
                            role="option"
                            aria-selected={globalIndex === activeIndex}
                            tabIndex={-1}
                            className={cn(
                              "rounded-md px-3 py-2 text-sm",
                              globalIndex === activeIndex
                                ? "bg-muted text-foreground"
                                : "text-foreground",
                            )}
                            onMouseDown={(event) => {
                              event.preventDefault();
                              navigate(result.to);
                              onClose();
                            }}
                          >
                            <div className="truncate font-medium">{result.title}</div>
                            {result.subtitle ? (
                              <div className="truncate text-xs text-muted-foreground">
                                {result.subtitle}
                              </div>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>
              )}

              {(contactsQuery.isError || conversationsQuery.isError) && (
                <p className="mt-2 px-2 text-xs text-muted-foreground">
                  Some results couldn't load.
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          <span>to move</span>
          <Kbd>Enter</Kbd>
          <span>to open</span>
          <Kbd>Esc</Kbd>
          <span>to close</span>
        </div>
      </div>
    </div>
  );
}

export function CommandPalette(): React.JSX.Element | null {
  const [open, setOpen] = React.useState(false);
  const previouslyFocusedRef = React.useRef<HTMLElement | null>(null);

  /** Remember what had focus BEFORE the dialog mounts. This has to happen in the
   * open HANDLER, not in an effect: PaletteDialog's own "focus the input" effect is a
   * CHILD effect and therefore runs BEFORE any effect in this component, so an effect
   * here would capture the palette's own input and restoring it on close would drop
   * focus to <body>. */
  const openPalette = React.useCallback(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    setOpen(true);
  }, []);

  React.useEffect(() => {
    listeners.add(openPalette);
    return () => {
      listeners.delete(openPalette);
    };
  }, [openPalette]);

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        setOpen((prev) => {
          if (prev) return false;
          previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
          return true;
        });
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Restore focus on close. The cleanup runs after PaletteDialog has unmounted, so the
  // element we focus is the one that opened the palette.
  React.useEffect(() => {
    if (!open) return;
    return () => {
      previouslyFocusedRef.current?.focus?.();
      previouslyFocusedRef.current = null;
    };
  }, [open]);

  if (!open) return null;

  return <PaletteDialog onClose={() => setOpen(false)} />;
}
