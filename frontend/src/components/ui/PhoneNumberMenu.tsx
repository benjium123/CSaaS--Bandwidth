import * as React from "react";
import { MessageSquare, Phone } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

export type PhoneNumberMenuProps = {
  /** The number this menu acts on, E.164. */
  e164: string;
  /** What to show; defaults to formatPhone(e164). */
  label?: React.ReactNode;
  /** Our number to place the call from. When omitted the softphone picks its default. */
  fromE164?: string | null;
  /** Called when the user picks "Text". Omit to hide the Text item. */
  onText?: (e164: string) => void;
  /** Read-only inbox / no send permission: both items are disabled with `disabledReason`
   *  as their title. */
  disabled?: boolean;
  disabledReason?: string;
  className?: string;
  /** Extra text for the trigger's accessible name, e.g. the contact's name. */
  ariaLabel?: string;
};

/**
 * Every phone number in the UI is clickable -> Text / Call menu.
 *
 * I keep the trigger enabled when `disabled` is true so the user can open the menu and
 * read the disabledReason from each item's title, rather than facing a dead control.
 */
export function PhoneNumberMenu({
  e164,
  label,
  fromE164,
  onText,
  disabled = false,
  disabledReason,
  className,
  ariaLabel,
}: PhoneNumberMenuProps): React.JSX.Element {
  const softphone = useSoftphone();
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);
  const menuRef = React.useRef<HTMLDivElement | null>(null);

  // Same outside-click pattern as FilterMenu/NewConversationMenu in ConversationList.
  React.useEffect(() => {
    if (!open) return undefined;

    function onMousedown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", onMousedown);
    return () => document.removeEventListener("mousedown", onMousedown);
  }, [open]);

  /** Escape is handled LOCALLY (React onKeyDown on this container) and the event is
   * stopped, deliberately: this menu is rendered inside surfaces that close themselves on
   * a document-level Escape - the mobile inbox Sheet, the contact Sheet, and
   * ConversationHeader's "more" menu. A document listener here would close the menu AND
   * the sheet behind it with one keypress. Do not move this back onto `document`. */
  function handleContainerKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape" || !open) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    setOpen(false);
    triggerRef.current?.focus();
  }

  // Focus the first enabled menu item when the menu opens.
  React.useEffect(() => {
    if (!open) return;
    const first = menuRef.current?.querySelector<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    );
    first?.focus();
  }, [open]);

  async function call() {
    try {
      await softphone.dial(e164, fromE164 ?? undefined);
    } catch {
      /* softphone surface already handles the visible error */
    }
    setOpen(false);
  }

  function handleMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    if (items.length === 0) return;

    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);

    function move(delta: number) {
      event.preventDefault();
      const next = (currentIndex + delta + items.length) % items.length;
      items[next].focus();
    }

    if (event.key === "ArrowDown") {
      move(1);
    } else if (event.key === "ArrowUp") {
      move(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
    } else if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
    }
  }

  return (
    <div className="relative" ref={containerRef} onKeyDown={handleContainerKeyDown}>
      <Button
        ref={triggerRef}
        type="button"
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel ?? `Actions for ${formatPhone(e164)}`}
        onClick={() => setOpen((value) => !value)}
        className={cn("justify-start", className)}
      >
        {label ?? formatPhone(e164)}
      </Button>

      {open && (
        <div
          role="menu"
          aria-label="Phone number actions"
          ref={menuRef}
          onKeyDown={handleMenuKeyDown}
          className="absolute left-0 top-full z-20 mt-1 w-40 rounded-md border border-neutral-700 bg-neutral-800 p-1 shadow-lg"
        >
          {onText && (
            <Button
              type="button"
              role="menuitem"
              variant="ghost"
              size="sm"
              disabled={disabled}
              title={disabled ? disabledReason : undefined}
              onClick={() => {
                onText(e164);
                setOpen(false);
              }}
              className="w-full justify-start rounded px-2 py-1 text-xs text-neutral-200 hover:bg-neutral-700 disabled:opacity-50"
            >
              <MessageSquare className="h-3.5 w-3.5" />
              Text
            </Button>
          )}
          <Button
            type="button"
            role="menuitem"
            variant="ghost"
            size="sm"
            disabled={disabled}
            title={disabled ? disabledReason : undefined}
            onClick={call}
            className="w-full justify-start rounded px-2 py-1 text-xs text-neutral-200 hover:bg-neutral-700 disabled:opacity-50"
          >
            <Phone className="h-3.5 w-3.5" />
            Call
          </Button>
        </div>
      )}
    </div>
  );
}
