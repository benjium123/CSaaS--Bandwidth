import * as React from "react";

import { useAuth } from "@/auth/AuthContext";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import {
  discountLabel,
  formatDiscountPercent,
  useOrgDiscounts,
  useRemoveOrgDiscount,
  useSetOrgDiscounts,
  type StripeSyncStatus,
} from "@/api/opsDiscounts";

/** Per-workspace discounts. A discount is a percentage off one or more charge categories; the
 * server owns the authoritative list and bills along it, so we never fake a state change
 * locally: every write is a PUT/DELETE and the list is refetched. Writes need an admin
 * operator; a reviewer gets a 403 that we surface verbatim rather than pretending it stuck. */

const STRIPE_MESSAGES: Record<NonNullable<StripeSyncStatus>, string> = {
  synced: "Stripe subscription updated.",
  no_subscription: "No Stripe subscription yet; it will apply when they subscribe.",
  failed: "Saved, but Stripe could not be updated. It will retry on their next plan change.",
};

export function OrgDiscountsPanel({
  orgId,
  canEdit,
}: {
  orgId: string;
  canEdit: boolean;
}): JSX.Element {
  const { api } = useAuth();

  const discountsQuery = useOrgDiscounts(api, orgId);
  const setMutation = useSetOrgDiscounts(api, orgId);
  const removeMutation = useRemoveOrgDiscount(api, orgId);

  const discounts = discountsQuery.data?.discounts ?? [];
  const categories = React.useMemo(
    () => discountsQuery.data?.categories ?? [],
    [discountsQuery.data],
  );

  const [selected, setSelected] = React.useState<string[]>([]);
  const [percent, setPercent] = React.useState("");
  const [endsOn, setEndsOn] = React.useState("");
  const [note, setNote] = React.useState("");
  const [stripeStatus, setStripeStatus] = React.useState<StripeSyncStatus>(null);

  const parsedPercent = Number(percent.trim());
  const canSubmit =
    selected.length > 0 &&
    percent.trim() !== "" &&
    Number.isFinite(parsedPercent) &&
    parsedPercent > 0 &&
    parsedPercent <= 100;

  function toggleCategory(category: string, checked: boolean) {
    if (checked) {
      setSelected((current) => (current.includes(category) ? current : [...current, category]));
      // Ticking a category that is already discounted fills in its percentage if the operator
      // has not typed one yet (editing an existing discount without retyping it).
      if (percent.trim() === "") {
        const existing = discounts.find((discount) => discount.category === category);
        if (existing) setPercent(formatDiscountPercent(existing.percent_bps));
      }
    } else {
      setSelected((current) => current.filter((item) => item !== category));
    }
  }

  function apply(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    // Send in catalog order; a category the backend knows but this bundle's catalog does not is
    // still sent, appended after the known ones.
    const ordered = categories.filter((category) => selected.includes(category));
    const extra = selected.filter((category) => !categories.includes(category));
    setMutation.mutate(
      {
        categories: [...ordered, ...extra],
        percent: parsedPercent,
        ends_at: endsOn ? `${endsOn}T23:59:59Z` : null,
        note: note.trim() === "" ? null : note.trim(),
      },
      {
        onSuccess: (result) => {
          setStripeStatus(result.stripe ?? null);
          setSelected([]);
          setPercent("");
          setEndsOn("");
          setNote("");
        },
      },
    );
  }

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Discounts</SectionLabel>
      <p className="text-xs text-muted-foreground">
        Take a percentage off what this workspace pays. Pick one or more charges; each keeps its
        own percentage.
      </p>

      {discountsQuery.isPending ? (
        <Spinner label="Loading discounts" />
      ) : discountsQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(discountsQuery.error)}
        </p>
      ) : discounts.length > 0 ? (
        <ul className="rounded-[var(--cx-r-md,14px)] border border-border">
          {discounts.map((discount) => {
            const meta = discountLabel(discount.category);
            return (
              <li
                key={discount.category}
                className="flex items-start justify-between gap-4 border-t border-border px-3 py-2 first:border-t-0"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium">{meta.label}</span>
                    <span className="text-[13px] font-medium">
                      {formatDiscountPercent(discount.percent_bps)}%
                    </span>
                    <span
                      className={
                        discount.active
                          ? "rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium"
                          : "rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
                      }
                    >
                      {discount.active ? "Active" : "Expired"}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {discount.ends_at
                        ? `Until ${discount.ends_at.slice(0, 10)}`
                        : "No end date"}
                    </span>
                  </div>
                  {discount.note ? (
                    <p className="text-xs text-muted-foreground">{discount.note}</p>
                  ) : null}
                </div>
                {canEdit ? (
                  <button
                    type="button"
                    aria-label={`Remove ${meta.label} discount`}
                    className="shrink-0 rounded-[var(--cx-r-md,14px)] border border-border px-2 py-1 text-[11px] font-medium text-destructive disabled:opacity-50"
                    disabled={
                      removeMutation.isPending &&
                      removeMutation.variables === discount.category
                    }
                    onClick={() => removeMutation.mutate(discount.category)}
                  >
                    Remove
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <ConsoleEmpty>No discounts. This workspace pays list prices.</ConsoleEmpty>
      )}

      {canEdit ? (
        <form className="space-y-3" onSubmit={apply}>
          <fieldset className="space-y-2">
            <legend className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Charges
            </legend>
            <div className="space-y-2">
              {categories.map((category) => {
                const meta = discountLabel(category);
                return (
                  <label
                    key={category}
                    className="flex items-start gap-2 rounded-[var(--cx-r-md,14px)] border border-border px-3 py-2"
                  >
                    <input
                      type="checkbox"
                      aria-label={meta.label}
                      className="mt-0.5"
                      checked={selected.includes(category)}
                      onChange={(event) => toggleCategory(category, event.target.checked)}
                    />
                    <span className="space-y-1">
                      <span className="block text-[13px] font-medium">{meta.label}</span>
                      {meta.help ? (
                        <span className="block text-xs text-muted-foreground">{meta.help}</span>
                      ) : null}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>

          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1 text-xs">
              <span className="block font-medium">Discount %</span>
              <input
                type="number"
                step="0.01"
                min="0.01"
                max="100"
                value={percent}
                onChange={(event) => setPercent(event.target.value)}
                className="w-full rounded-[var(--cx-r-md,14px)] border border-border bg-background px-2 py-1"
              />
            </label>
            <label className="space-y-1 text-xs">
              <span className="block font-medium">Ends on</span>
              <input
                type="date"
                value={endsOn}
                onChange={(event) => setEndsOn(event.target.value)}
                className="w-full rounded-[var(--cx-r-md,14px)] border border-border bg-background px-2 py-1"
              />
            </label>
            <label className="space-y-1 text-xs">
              <span className="block font-medium">Note</span>
              <input
                type="text"
                maxLength={255}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                className="w-full rounded-[var(--cx-r-md,14px)] border border-border bg-background px-2 py-1"
              />
            </label>
          </div>

          <button
            type="submit"
            disabled={!canSubmit || setMutation.isPending}
            className="rounded-[var(--cx-r-md,14px)] border border-border bg-muted px-3 py-1 text-[13px] font-medium disabled:opacity-50"
          >
            Apply discount
          </button>
        </form>
      ) : null}

      {stripeStatus ? (
        <p
          role="status"
          className={
            stripeStatus === "failed"
              ? "text-sm text-amber-600"
              : "text-xs text-muted-foreground"
          }
        >
          {STRIPE_MESSAGES[stripeStatus]}
        </p>
      ) : null}

      {removeMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(removeMutation.error)}
        </p>
      ) : null}

      {setMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(setMutation.error)}
        </p>
      ) : null}
    </SurfaceCard>
  );
}
