import * as React from "react";
import { Loader2, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useContacts } from "@/api/hooks";
import { formatPhone, normalizePhoneToE164 } from "@/lib/format";
import type { FromOption } from "@/components/conversations/NewConversationPanel";
import { cn } from "@/lib/utils";

const OPTIONS_LISTBOX_ID = "start-call-options-listbox";
/** Same threshold as NewConversationPanel's own contacts search - fewer than 2
 * characters would fetch a near-unfiltered contacts list on every keystroke, including
 * the very first render before the user has typed anything. */
const MIN_SEARCH_CHARS = 2;

/** F20-style debounce (same pattern as NewConversationPanel's own copy). Kept local
 * rather than shared - see the comment on NewConversationPanel's copy for why. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

interface StartCallOption {
  id: string;
  label: string;
  to: string;
}

/**
 * The Quo/OpenPhone-style "Start a call" floating dialog: a compact card over the inbox
 * with a line picker ("Make the call from") and one big "Enter a name or phone number"
 * field. Selecting a contact suggestion or a typed number places the call immediately -
 * there is no separate submit button.
 *
 * This replaces NewConversationPanel for kind="call" only; NewConversationPanel is still
 * used for kind="message" (see ConversationsPage).
 */
export function StartCallDialog({
  fromOptions,
  initialTo = null,
  initialFrom = null,
  onCancel,
  onCall,
}: {
  fromOptions: FromOption[];
  /** Prefill the query field, e.g. from `/inbox?compose=<e164>`. */
  initialTo?: string | null;
  /** Prefill the From line when the caller knows which of our numbers to use. */
  initialFrom?: string | null;
  onCancel: () => void;
  onCall: (vars: { from: string; to: string }) => Promise<void>;
}) {
  const { api } = useAuth();
  const [from, setFrom] = React.useState(initialFrom ?? fromOptions[0]?.e164 ?? "");
  const [query, setQuery] = React.useState(initialTo ? formatPhone(initialTo) : "");
  const [open, setOpen] = React.useState(false);
  const [highlightedIndex, setHighlightedIndex] = React.useState(-1);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const dialogRef = React.useRef<HTMLDivElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);

  // The From select's first option is only known once fromOptions has loaded - keep it
  // in sync rather than freezing on the empty string from the initial render (mirrors
  // NewConversationPanel).
  React.useEffect(() => {
    if (!from && fromOptions.length > 0) setFrom(fromOptions[0].e164);
  }, [from, fromOptions]);

  // Autofocused per the reference: the whole point of this dialog is to start typing a
  // name or number right away.
  React.useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Esc or a click outside the card both cancel - same outside-click pattern used
  // elsewhere in this file family (NewConversationPanel's picker, ConversationHeader's
  // menus), just scoped to the whole dialog instead of a sub-picker.
  React.useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    function onPointerDown(e: MouseEvent) {
      if (dialogRef.current && !dialogRef.current.contains(e.target as Node)) onCancel();
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [onCancel]);

  const debouncedQuery = useDebouncedValue(query, 300);
  const trimmedQuery = debouncedQuery.trim();
  const searchEnabled = trimmedQuery.length >= MIN_SEARCH_CHARS;
  const contactsQuery = useContacts(api, trimmedQuery, searchEnabled);

  // Computed off the RAW (non-debounced) query, deliberately: this is a pure local
  // parse, not a network call, so a typed number should show its "Call ..." option the
  // instant it becomes valid rather than waiting out the contacts-search debounce.
  const directE164 = normalizePhoneToE164(query);

  const options = React.useMemo<StartCallOption[]>(() => {
    const contactOptions: StartCallOption[] = searchEnabled
      ? (contactsQuery.data ?? []).flatMap((contact) =>
          contact.phones.map((phone) => ({
            id: `start-call-contact-${contact.id}-${phone.e164}`,
            label: `${contact.display_name} · ${formatPhone(phone.e164)}`,
            to: phone.e164,
          })),
        )
      : [];
    if (!directE164) return contactOptions;
    // A number already offered as a saved contact isn't repeated as a bare dial option.
    if (contactOptions.some((opt) => opt.to === directE164)) return contactOptions;
    return [
      ...contactOptions,
      { id: "start-call-dial-direct", label: `Call ${formatPhone(directE164)}`, to: directE164 },
    ];
  }, [searchEnabled, contactsQuery.data, directE164]);

  const expanded = open && options.length > 0;

  // The highlighted option must never point past the end of a shrinking result set.
  React.useEffect(() => {
    if (highlightedIndex >= options.length) setHighlightedIndex(options.length > 0 ? 0 : -1);
  }, [options, highlightedIndex]);

  async function placeCall(to: string) {
    if (!from || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onCall({ from, to });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function handleQueryChange(value: string) {
    setQuery(value);
    setOpen(true);
    setHighlightedIndex(-1);
  }

  function handleQueryKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (options.length > 0) setHighlightedIndex((i) => (i + 1) % options.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (options.length > 0) setHighlightedIndex((i) => (i - 1 + options.length) % options.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      // A highlighted suggestion (arrowed to) wins; otherwise a directly-typed valid
      // number calls itself - the primary "type a number, hit Enter" flow needs no
      // navigation at all.
      if (expanded && highlightedIndex >= 0 && options[highlightedIndex]) {
        void placeCall(options[highlightedIndex].to);
      } else if (directE164) {
        void placeCall(directE164);
      }
    }
    // Escape is handled by the document-level listener above (it also closes the whole
    // dialog, not just this listbox), so nothing extra to do here.
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label="Start a call"
      className="fixed left-1/2 top-16 z-50 w-full max-w-sm -translate-x-1/2 rounded-2xl border border-border bg-background p-4 shadow-xl"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-foreground">Start a call</h2>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Close"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
        <span>Make the call from</span>
        <select
          aria-label="Call from"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          disabled={fromOptions.length === 0 || busy}
          className="h-8 rounded-full border border-border bg-muted px-3 text-sm font-medium text-foreground focus:outline-none focus:ring-1 focus:ring-muted-foreground disabled:opacity-50"
        >
          {fromOptions.length === 0 && <option value="">No numbers available</option>}
          {fromOptions.map((opt) => (
            <option key={opt.e164} value={opt.e164}>
              {opt.label} · {formatPhone(opt.e164)}
            </option>
          ))}
        </select>
      </div>

      <div className="relative">
        <input
          ref={inputRef}
          aria-label="Enter a name or phone number"
          role="combobox"
          aria-expanded={expanded}
          aria-controls={OPTIONS_LISTBOX_ID}
          aria-autocomplete="list"
          aria-activedescendant={
            expanded && highlightedIndex >= 0 && options[highlightedIndex]
              ? options[highlightedIndex].id
              : undefined
          }
          placeholder="Enter a name or phone number…"
          value={query}
          onChange={(e) => handleQueryChange(e.target.value)}
          onFocus={() => setOpen(true)}
          onKeyDown={handleQueryKeyDown}
          autoComplete="off"
          disabled={busy}
          className="w-full border-0 border-b border-border bg-transparent pb-2 text-lg text-foreground placeholder:text-muted-foreground focus:outline-none disabled:opacity-50"
        />
        {expanded && (
          <ul
            id={OPTIONS_LISTBOX_ID}
            role="listbox"
            aria-label="Suggestions"
            className="absolute left-0 right-0 top-full z-20 mt-1 max-h-56 overflow-y-auto rounded-md border border-border bg-muted p-1 shadow-lg"
          >
            {options.map((opt, index) => (
              <li
                key={opt.id}
                id={opt.id}
                role="option"
                aria-selected={index === highlightedIndex}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => placeCall(opt.to)}
                className={cn(
                  "cursor-pointer rounded px-2 py-1.5 text-sm text-foreground hover:bg-foreground/10",
                  index === highlightedIndex && "bg-foreground/10",
                )}
              >
                {opt.label}
              </li>
            ))}
          </ul>
        )}
      </div>

      {busy && (
        <p role="status" className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Calling…
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
