/**
 * Page chrome for the console's non-inbox pages.
 *
 * THE PALETTE AND THE RADII ARE NOT INVENTED HERE. Every value is read off
 * docs/design/console-reference.html, the design the operator signed off:
 *
 *   radius   999px pills/badges/primary actions · 18px large surfaces · 14px cards and
 *            rows · 12px list rows · 10px nav items and icon buttons · 50% avatars.
 *            Nothing on a console page is square.
 *   colour   `--cx-*` tokens only. accent (azure) is the primary action, live (green) is
 *            a call, flag (yellow) is a note or a warning. No orange, no raw Tailwind
 *            palette hue.
 *   density  9-14px padding, 11-12px gaps. The reference is not a dense table.
 *
 * The tokens live on `.console-surface` (components/conversations/consoleTheme.css), which
 * the Shell already carries, so these components only work inside the console — which is
 * the only place they are used.
 *
 * `.cx-avatar` is that same stylesheet's avatar: a 145deg gradient with a stable per-seed
 * hue. `InitialsAvatar` is a thin wrapper so a page listing people does not each
 * re-implement the initials + hue pair.
 */
import * as React from "react";

import { avatarHueIndex, initialsOf } from "@/lib/format";
import { cn } from "@/lib/utils";

/** The reference's `.sec`: 11.5px/600, muted. Section headings on every page. */
export function SectionLabel({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]",
        className,
      )}
      {...props}
    />
  );
}

/** A large surface — the reference's 18px radius on `surface` over a `line` border. */
export const SurfaceCard = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function SurfaceCard({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        "rounded-[18px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-[18px]",
        className,
      )}
      {...props}
    />
  );
});

/** A card or a row inside a surface: 14px, recessed onto `overlay`. */
export const ConsoleCard = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function ConsoleCard({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        "rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-[14px]",
        className,
      )}
      {...props}
    />
  );
});

/**
 * The page's top bar: title, an optional line of explanation, and the page's actions.
 * `as` keeps the heading level where the page had it — several suites find a page by its
 * `heading` role and name, and changing the level would move them.
 */
export function PageHeader({
  title,
  description,
  actions,
  headingLevel = 1,
  className,
  children,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  headingLevel?: 1 | 2 | 3;
  className?: string;
  children?: React.ReactNode;
}) {
  const Heading = `h${headingLevel}` as "h1" | "h2" | "h3";
  return (
    <div className={cn("flex flex-col gap-[11px]", className)}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <Heading className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
            {title}
          </Heading>
          {description ? (
            <p className="mt-1 text-[12.5px] text-[hsl(var(--cx-muted))]">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        ) : null}
      </div>
      {children}
    </div>
  );
}

/**
 * A filter chip. The reference's `.pill`: 999px, 6/14 padding, `overlay` at rest and a
 * solid accent fill when pressed. `aria-pressed` stays the caller's to set — it is how
 * the existing suites find these.
 */
export const FilterPill = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }
>(function FilterPill({ className, active, ...props }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      className={cn(
        "rounded-full px-[14px] py-[6px] text-[12.5px] font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--cx-accent))]",
        active
          ? "bg-[hsl(var(--cx-accent))] font-semibold text-[hsl(var(--cx-on-acc))]"
          : "bg-[hsl(var(--cx-overlay))] text-[hsl(var(--cx-subtle))] hover:bg-[hsl(var(--cx-lift))] hover:text-[hsl(var(--cx-text))]",
        className,
      )}
      {...props}
    />
  );
});

const AVATAR_SIZES = {
  sm: "h-7 w-7 text-[10.5px]",
  md: "h-[34px] w-[34px] text-[12px]",
  lg: "h-[38px] w-[38px] text-[13px]",
  xl: "h-16 w-16 text-[21px]",
} as const;

/**
 * Two-letter initials on the reference's 145deg gradient, in a circle.
 *
 * `seed` is what picks the hue and should be something IMMUTABLE — a contact id, an
 * E.164 — so renaming someone does not repaint them. It falls back to the name only
 * when there is nothing stabler.
 *
 * Decorative by default: the name it draws is always present as text beside it, so
 * announcing the initials again would just be noise.
 */
export function InitialsAvatar({
  name,
  seed,
  size = "md",
  className,
}: {
  name: string;
  seed?: string;
  size?: keyof typeof AVATAR_SIZES;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      data-hue={avatarHueIndex(seed || name)}
      className={cn(
        "cx-avatar grid flex-none place-items-center rounded-full",
        AVATAR_SIZES[size],
        className,
      )}
    >
      {initialsOf(name)}
    </span>
  );
}

/** An empty/placeholder well: dashed 14px, generous padding, muted type. */
export function ConsoleEmpty({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-[14px] border border-dashed border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-6 text-[13px] text-[hsl(var(--cx-muted))]",
        className,
      )}
      {...props}
    />
  );
}
