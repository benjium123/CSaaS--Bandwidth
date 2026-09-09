import * as React from "react";
import { Loader2, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import { useContacts } from "@/api/hooks";
import { estimateSmsSegments, formatPhone, normalizePhoneToE164 } from "@/lib/format";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type NewConversationKind = "message" | "call";

export interface FromOption {
  e164: string;
  label: string;
}

const TO_LISTBOX_ID = "new-convo-to-listbox";
/** Contacts search only kicks in once the query is long enough to be meaningful - fewer
 * than 2 characters would otherwise fetch (and re-fetch) a near-unfiltered contacts list
 * on every keystroke, including the very first render before the user has typed anything. */
const MIN_SEARCH_CHARS = 2;

interface ToOption {
  id: string;
  displayName: string;
  phone: string;
}

/** F20-style debounce (same pattern as ConversationsPage/InboxPage's own
 * useDebouncedValue) - typing in the To field shouldn't hit /contacts on every
 * keystroke. Kept local rather than shared, matching how the other two pages each keep
 * their own copy. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/**
 * Inline "New text message" / "New call" compose panel - replaces the right pane's
 * "Select a conversation" placeholder while composing to a not-yet-selected pair.
 *
 * From/To resolution happens entirely here; the caller (ConversationsPage) only gets a
 * fully-resolved {from, to} pair once the user actually submits.
 */
export function NewConversationPanel({
  kind,
  fromOptions,
  onCancel,
  onSendMessage,
  onCall,
}: {
  kind: NewConversationKind;
  fromOptions: FromOption[];
  onCancel: () => void;
  onSendMessage: (vars: {
    from: string;
    to: string;
    body: string;
    allowReassign: boolean;
  }) => Promise<void>;
  onCall: (vars: { from: string; to: string }) => Promise<void>;
}) {
  const { api } = useAuth();
  const [from, setFrom] = React.useState(fromOptions[0]?.e164 ?? "");
  const [toQuery, setToQuery] = React.useState("");
  const [toE164, setToE164] = React.useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = React.useState(false);
  const [highlightedIndex, setHighlightedIndex] = React.useState(-1);
  const [body, setBody] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  // Mirrors Composer.tsx's own sticky-sender-unavailable confirm - a 422 here means this
  // From number was retired between picking it and hitting Send; resubmit with
  // allow_reassign rather than silently failing or guessing a different number.
  const [needsReassign, setNeedsReassign] = React.useState(false);
  const reassignButtonRef = React.useRef<HTMLButtonElement>(null);
  const pickerRef = React.useRef<HTMLDivElement>(null);

  // The From select's first option is only known once fromOptions has loaded - keep it
  // in sync rather than freezing on the empty string from the initial render.
  React.useEffect(() => {
    if (!from && fromOptions.length > 0) setFrom(fromOptions[0].e164);
  }, [from, fromOptions]);

  React.useEffect(() => {
    if (needsReassign) reassignButtonRef.current?.focus();
  }, [needsReassign]);

  const debouncedQuery = useDebouncedValue(toQuery, 300);
  const trimmedQuery = debouncedQuery.trim();
  // Item 4: never search once a contact/number is already resolved (toE164 set), and
  // never search on too-short input - avoids fetching the whole contacts list on mount
  // and on every early keystroke.
  const searchEnabled = !toE164 && trimmedQuery.length >= MIN_SEARCH_CHARS;
  const contactsQuery = useContacts(api, trimmedQuery, searchEnabled);
  const options = React.useMemo<ToOption[]>(() => {
    if (!searchEnabled) return [];
    return (contactsQuery.data ?? []).flatMap((contact) =>
      contact.phones.map((phone) => ({
        id: `new-convo-to-option-${contact.id}-${phone.e164}`,
        displayName: contact.display_name,
        phone: phone.e164,
      })),
    );
  }, [searchEnabled, contactsQuery.data]);
  const expanded = pickerOpen && options.length > 0;

  React.useEffect(() => {
    if (!pickerOpen) return undefined;
    function onPointerDown(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setPickerOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [pickerOpen]);

  // The highlighted option must never point past the end of a shrinking result set.
  React.useEffect(() => {
    if (highlightedIndex >= options.length) setHighlightedIndex(options.length > 0 ? 0 : -1);
  }, [options, highlightedIndex]);

  function selectContact(displayName: string, phone: string) {
    setToE164(phone);
    setToQuery(`${displayName} · ${formatPhone(phone)}`);
    setPickerOpen(false);
    setHighlightedIndex(-1);
  }

  function handleToChange(value: string) {
    setToQuery(value);
    setToE164(null);
    setPickerOpen(true);
    setHighlightedIndex(-1);
  }

  function handleToKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!pickerOpen) {
        setPickerOpen(true);
        return;
      }
      if (options.length > 0) setHighlightedIndex((i) => (i + 1) % options.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!pickerOpen) {
        setPickerOpen(true);
        return;
      }
      if (options.length > 0) setHighlightedIndex((i) => (i - 1 + options.length) % options.length);
    } else if (e.key === "Enter") {
      if (expanded && highlightedIndex >= 0 && options[highlightedIndex]) {
        // Selecting a highlighted suggestion, not submitting the form.
        e.preventDefault();
        const opt = options[highlightedIndex];
        selectContact(opt.displayName, opt.phone);
      }
    } else if (e.key === "Escape") {
      setPickerOpen(false);
    }
  }

  // Resolves whatever the user typed (a contact pick, or raw digits) to a real E.164 -
  // null means "not resolvable yet", which keeps Send/Call disabled.
  const resolvedTo = toE164 ?? normalizePhoneToE164(toQuery);

  const segments = React.useMemo(() => estimateSmsSegments(body), [body]);
  const canSubmit =
    Boolean(from) && Boolean(resolvedTo) && (kind === "call" || body.trim().length > 0);

  async function attemptSend(allowReassign: boolean) {
    if (!resolvedTo || !from) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "message") {
        await onSendMessage({ from, to: resolvedTo, body: body.trim(), allowReassign });
        setNeedsReassign(false);
      } else {
        await onCall({ from, to: resolvedTo });
      }
    } catch (err) {
      if (kind === "message" && err instanceof ApiError && err.code === "sticky_sender_unavailable") {
        setNeedsReassign(true);
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await attemptSend(false);
  }

  return (
    <form
      onSubmit={submit}
      className="flex min-w-0 flex-1 flex-col overflow-y-auto p-4"
      aria-label={kind === "message" ? "New text message" : "New call"}
    >
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-neutral-50">
          {kind === "message" ? "New text message" : "New call"}
        </h2>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Cancel"
          className="rounded-md p-1.5 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-50"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-3">
        <div>
          <label htmlFor="new-convo-from" className="mb-1 block text-xs text-neutral-400">
            From
          </label>
          <select
            id="new-convo-from"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            disabled={fromOptions.length === 0}
            className="h-9 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
          >
            {fromOptions.length === 0 && <option value="">No numbers available</option>}
            {fromOptions.map((opt) => (
              <option key={opt.e164} value={opt.e164}>
                {opt.label} · {formatPhone(opt.e164)}
              </option>
            ))}
          </select>
        </div>

        <div className="relative" ref={pickerRef}>
          <label htmlFor="new-convo-to" className="mb-1 block text-xs text-neutral-400">
            To
          </label>
          <input
            id="new-convo-to"
            aria-label="To"
            type="text"
            role="combobox"
            aria-expanded={expanded}
            aria-controls={TO_LISTBOX_ID}
            aria-autocomplete="list"
            aria-activedescendant={
              expanded && highlightedIndex >= 0 && options[highlightedIndex]
                ? options[highlightedIndex].id
                : undefined
            }
            placeholder="Name or phone number"
            value={toQuery}
            onChange={(e) => handleToChange(e.target.value)}
            onFocus={() => setPickerOpen(true)}
            onKeyDown={handleToKeyDown}
            autoComplete="off"
            className="h-9 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
          />
          {expanded && (
            <ul
              id={TO_LISTBOX_ID}
              role="listbox"
              aria-label="Matching contacts"
              className="absolute left-0 right-0 top-full z-20 mt-1 max-h-48 overflow-y-auto rounded-md border border-neutral-700 bg-neutral-800 p-1 shadow-lg"
            >
              {options.map((opt, index) => (
                <li
                  key={opt.id}
                  id={opt.id}
                  role="option"
                  aria-selected={index === highlightedIndex}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => selectContact(opt.displayName, opt.phone)}
                  className={cn(
                    "cursor-pointer rounded px-2 py-1 text-xs text-neutral-200 hover:bg-neutral-700",
                    index === highlightedIndex && "bg-neutral-700",
                  )}
                >
                  {opt.displayName} · {formatPhone(opt.phone)}
                </li>
              ))}
            </ul>
          )}
          {toQuery.trim() !== "" && !resolvedTo && (
            <p className="mt-1 text-[11px] text-neutral-500">
              Pick a contact or enter a valid phone number
            </p>
          )}
        </div>

        {kind === "message" && (
          <div>
            <label htmlFor="new-convo-body" className="mb-1 block text-xs text-neutral-400">
              Message
            </label>
            <textarea
              id="new-convo-body"
              aria-label="Message"
              placeholder="Type a message"
              value={body}
              rows={4}
              onChange={(e) => setBody(e.target.value)}
              className="w-full resize-y rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-sm text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
            />
            <p className="mt-1 text-[11px] text-neutral-500">
              {segments.units} char{segments.units === 1 ? "" : "s"} · {segments.encoding} ·{" "}
              {segments.segments} segment{segments.segments === 1 ? "" : "s"}
            </p>
          </div>
        )}

        {kind === "message" && needsReassign && (
          <div
            role="alert"
            className="flex items-center justify-between gap-3 rounded-md border border-neutral-700 bg-neutral-800 p-2 text-xs text-neutral-200"
          >
            <span>This number was retired. Send from a new number?</span>
            <div className="flex gap-2">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setNeedsReassign(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                ref={reassignButtonRef}
                size="sm"
                onClick={() => attemptSend(true)}
              >
                Send anyway
              </Button>
            </div>
          </div>
        )}

        {error && (
          <p role="alert" className="text-xs text-red-400">
            {error}
          </p>
        )}

        <Button type="submit" disabled={!canSubmit || busy} className="w-full">
          {busy && <Loader2 className={cn("h-4 w-4 animate-spin")} />}
          {kind === "message" ? "Send" : "Call"}
        </Button>
      </div>
    </form>
  );
}
