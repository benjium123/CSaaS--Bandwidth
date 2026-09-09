/**
 * Minimal shadcn-style primitives, vendored by hand.
 *
 * Deliberately small: the shadcn CLI wants an interactive init and pulls a radix tree we
 * do not need for P2's surface. These are the same cva+tailwind pattern it generates.
 */
import { cva, type VariantProps } from "class-variance-authority";
import { ChevronDown, X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:opacity-90",
        outline: "border border-border bg-background hover:bg-muted",
        ghost: "hover:bg-muted",
        destructive: "bg-destructive text-white hover:opacity-90",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 px-3 text-xs",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  ),
);
Button.displayName = "Button";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "flex h-9 w-full rounded-md border border-border bg-background px-3 py-1 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export function Badge({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  // Spreads the rest so aria-label / title reach the DOM - a badge is often the only
  // thing announcing a count, so dropping aria-label would be an accessibility hole.
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="p-4 text-sm text-muted-foreground">
      {label}...
    </div>
  );
}

export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "flex h-9 w-full rounded-md border border-border bg-background px-3 py-1 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Select.displayName = "Select";

export type PillTone = "neutral" | "success" | "warning" | "danger" | "info";

const PILL_TONES: Record<PillTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  success: "bg-emerald-500/15 text-emerald-300",
  warning: "bg-amber-500/15 text-amber-300",
  danger: "bg-red-500/15 text-red-300",
  info: "bg-sky-500/15 text-sky-300",
};

type PillProps = React.HTMLAttributes<HTMLSpanElement> & {
  tone?: PillTone;
};

export const Pill = React.forwardRef<HTMLSpanElement, PillProps>(
  ({ tone = "neutral", className, children, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px]",
        PILL_TONES[tone],
        className,
      )}
      {...props}
    >
      {children}
    </span>
  ),
);
Pill.displayName = "Pill";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, children, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("rounded-lg border border-border bg-background p-4", className)}
      {...props}
    >
      {children}
    </div>
  ),
);
Card.displayName = "Card";

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        {description != null ? (
          <div className="text-xs text-muted-foreground">{description}</div>
        ) : null}
      </div>
      {actions != null ? (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}

type SectionProps = React.HTMLAttributes<HTMLElement> & {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
};

export const Section = React.forwardRef<HTMLElement, SectionProps>(
  ({ title, description, actions, children, className, ...props }, ref) => {
    const titleId = React.useId();
    return (
      <section ref={ref} {...props} aria-labelledby={titleId} className={cn("space-y-3", className)}>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold">
              {title}
            </h2>
            {description != null ? (
              <div className="text-xs text-muted-foreground">{description}</div>
            ) : null}
          </div>
          {actions != null ? (
            <div className="flex shrink-0 items-center gap-2">{actions}</div>
          ) : null}
        </div>
        {children}
      </section>
    );
  },
);
Section.displayName = "Section";

type EmptyStateProps = React.HTMLAttributes<HTMLDivElement> & {
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  icon?: React.ReactNode;
};

export const EmptyState = React.forwardRef<HTMLDivElement, EmptyStateProps>(
  ({ title, description, action, icon, className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-border p-8 text-center",
        className,
      )}
      {...props}
    >
      {icon != null ? <div className="mb-3 text-muted-foreground">{icon}</div> : null}
      <p className="text-sm font-medium">{title}</p>
      {description != null ? (
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      ) : null}
      {action != null ? <div className="mt-4">{action}</div> : null}
    </div>
  ),
);
EmptyState.displayName = "EmptyState";

export function mutationErrorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

type MutationStatusProps = {
  pending?: boolean;
  error?: unknown;
  success?: React.ReactNode;
  pendingLabel?: string;
  className?: string;
};

export function MutationStatus({
  pending = false,
  error,
  success,
  pendingLabel = "Saving…",
  className,
}: MutationStatusProps) {
  if (error) {
    return (
      <div role="alert" className={cn("text-sm text-destructive", className)}>
        {mutationErrorMessage(error)}
      </div>
    );
  }

  if (pending) {
    return (
      <div role="status" aria-live="polite" className={cn("text-sm text-muted-foreground", className)}>
        {pendingLabel}
      </div>
    );
  }

  if (success !== undefined && success !== null && success !== false) {
    return (
      <div role="status" aria-live="polite" className={cn("text-sm text-muted-foreground", className)}>
        {success}
      </div>
    );
  }

  return null;
}

type DrawerProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: string;
};

type SheetProps = DrawerProps & {
  side?: "bottom" | "left";
};

function Overlay({
  open,
  onClose,
  title,
  children,
  footer,
  panelClassName,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  panelClassName?: string;
}) {
  const titleId = React.useId();
  const closeRef = React.useRef<HTMLButtonElement | null>(null);
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  /** P20b (gap A1): the element that had focus when the dialog opened, so that closing -
   * by Escape, the backdrop, or the Close button - hands focus back to it instead of
   * dumping the user at the top of the document. Held in a ref so the restore survives
   * the re-render that unmounts the panel. */
  const openerRef = React.useRef<HTMLElement | null>(null);

  React.useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => {
      const opener = openerRef.current;
      openerRef.current = null;
      // Only restore when the opener is still on the page: a dialog that closed because
      // its whole surface unmounted has nothing left to hand focus back to, and calling
      // focus() on a detached node would silently move focus to <body> instead.
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [open]);

  React.useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  /** P20b (gap A1): a real focus trap. Tab from the last focusable wraps to the first and
   * Shift+Tab from the first wraps to the last, so keyboard focus cannot walk out of an
   * aria-modal dialog onto the inert page behind it. Deliberately NOT filtered by
   * visibility: jsdom has no layout, so an offsetParent/getClientRects check would empty
   * the list in tests while working in the browser - a trap that silently does nothing is
   * worse than none. */
  function handlePanelKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const panel = panelRef.current;
    if (!panel) return;

    const focusable = Array.from(
      panel.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusable.length === 0) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;

    if (event.shiftKey) {
      if (active === first || !panel.contains(active)) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (active === last || !panel.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  if (!open) return null;

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={handlePanelKeyDown}
        className={cn(
          "fixed z-50 flex flex-col bg-background border-border",
          panelClassName,
        )}
      >
        <div className="flex items-start justify-between border-b border-border p-4">
          <h2 id={titleId} className="text-sm font-semibold">
            {title}
          </h2>
          <Button
            ref={closeRef}
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Close"
            onClick={onClose}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer != null ? <div className="border-t border-border p-4">{footer}</div> : null}
      </div>
    </>
  );
}

export function Drawer({ open, onClose, title, children, footer, width }: DrawerProps) {
  return (
    <Overlay
      open={open}
      onClose={onClose}
      title={title}
      footer={footer}
      panelClassName={cn("right-0 top-0 h-full w-[420px] max-w-full border-l", width)}
    >
      {children}
    </Overlay>
  );
}

export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
  width,
  side = "bottom",
}: SheetProps) {
  return (
    <Overlay
      open={open}
      onClose={onClose}
      title={title}
      footer={footer}
      panelClassName={cn(
        side === "bottom"
          ? "inset-x-0 bottom-0 max-h-[85vh] rounded-t-xl border-t"
          : "inset-y-0 left-0 h-full w-[280px] max-w-[85vw] border-r",
        side === "left" ? width : undefined,
      )}
    >
      {children}
    </Overlay>
  );
}

type TabItem = {
  id: string;
  label: React.ReactNode;
  disabled?: boolean;
};

export function tabId(base: string, id: string): string {
  return `${base}-tab-${id}`;
}

export function panelId(base: string, id: string): string {
  return `${base}-panel-${id}`;
}

type TabsProps = {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  className?: string;
  id?: string;
};

export function Tabs({
  tabs,
  value,
  onChange,
  ariaLabel,
  className,
  id,
}: TabsProps) {
  const autoId = React.useId();
  const baseId = id ?? autoId;
  const tabRefs = React.useRef<Record<string, HTMLButtonElement | null>>({});

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const enabledIndexes = tabs
      .map((tab, index) => (tab.disabled ? null : index))
      .filter((index): index is number => index !== null);

    if (enabledIndexes.length === 0) return;

    const currentEnabledIndex = enabledIndexes.indexOf(
      tabs.findIndex((tab) => tab.id === value),
    );

    function move(delta: number) {
      event.preventDefault();
      const nextIndex =
        (currentEnabledIndex + delta + enabledIndexes.length) % enabledIndexes.length;
      const nextTab = tabs[enabledIndexes[nextIndex]];
      onChange(nextTab.id);
      tabRefs.current[nextTab.id]?.focus();
    }

    if (event.key === "ArrowRight") {
      move(1);
    } else if (event.key === "ArrowLeft") {
      move(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      const nextTab = tabs[enabledIndexes[0]];
      onChange(nextTab.id);
      tabRefs.current[nextTab.id]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      const nextTab = tabs[enabledIndexes[enabledIndexes.length - 1]];
      onChange(nextTab.id);
      tabRefs.current[nextTab.id]?.focus();
    }
  }

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn("flex", className)}
      onKeyDown={handleKeyDown}
    >
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            ref={(node) => {
              tabRefs.current[tab.id] = node;
            }}
            type="button"
            role="tab"
            id={tabId(baseId, tab.id)}
            aria-selected={selected}
            aria-controls={panelId(baseId, tab.id)}
            tabIndex={selected ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => onChange(tab.id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm",
              selected
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted",
              tab.disabled && "opacity-50",
            )}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  tabsId,
  id,
  children,
}: {
  tabsId: string;
  id: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={panelId(tabsId, id)}
      aria-labelledby={tabId(tabsId, id)}
      tabIndex={0}
    >
      {children}
    </div>
  );
}

export function useCollapsible(
  storageKey: string,
  defaultOpen?: boolean,
): [boolean, (open: boolean) => void] {
  const [open, setOpen] = React.useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(storageKey);
      if (stored === "false") return false;
      if (stored === "true") return true;
    } catch {
      // Private mode / storage disabled - default to open rather than crashing the shell.
    }
    return defaultOpen ?? true;
  });

  const setOpenPersist = React.useCallback(
    (next: boolean) => {
      setOpen(next);
      try {
        localStorage.setItem(storageKey, String(next));
      } catch {
        // Private mode - the collapsed state simply won't persist.
      }
    },
    [storageKey],
  );

  return [open, setOpenPersist];
}

type CollapsibleProps = {
  storageKey: string;
  title: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
  className?: string;
};

export function Collapsible({
  storageKey,
  title,
  children,
  defaultOpen,
  className,
}: CollapsibleProps) {
  const [open, setOpen] = useCollapsible(storageKey, defaultOpen);
  const contentId = React.useId();

  return (
    <div className={cn("rounded-lg border border-border", className)}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between rounded-t-lg px-4 py-3 text-sm font-medium hover:bg-muted"
      >
        <span>{title}</span>
        <ChevronDown
          className={cn("h-4 w-4 transition-transform", !open && "-rotate-90")}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div id={contentId} className="border-t border-border p-4">
          {children}
        </div>
      ) : null}
    </div>
  );
}

export const Kbd = React.forwardRef<HTMLElement, React.HTMLAttributes<HTMLElement>>(
  ({ className, children, ...props }, ref) => (
    <kbd
      ref={ref}
      className={cn(
        "rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground",
        className,
      )}
      {...props}
    >
      {children}
    </kbd>
  ),
);
Kbd.displayName = "Kbd";
