import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, mutationErrorMessage } from "@/components/ui/primitives";
import { dollars, packQuote, useBuyPack, useRemovePack } from "@/api/plan";
import type { BillingInterval, NumberPack, WorkspacePlan } from "@/api/plan";

type Confirmation =
  | { kind: "add"; code: string; size: number; cents: number }
  | { kind: "remove"; code: string; size: number };

/** Settings > Billing: number packs, which cost less per number than $5 add-ons. A pack is only
 *  bought once the exact change to this period's bill has been shown and accepted. */
export function NumberPacks({ data, interval }: { data: WorkspacePlan; interval?: BillingInterval }) {
  const { api } = useAuth();
  const buy = useBuyPack(api);
  const remove = useRemovePack(api);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  const packs = data.number_packs ?? [];
  const shown = packs.filter(p => p.available || p.owned > 0);
  if (shown.length === 0) return null;

  const period = interval === "year" ? "year" : "month";
  const cheapest = packs
    .filter(p => p.available)
    .reduce<NumberPack | null>((best, p) => (best === null || p.price_cents < best.price_cents ? p : best), null);
  const extras = data.numbers.extra ?? 0;
  const extrasCents = extras * data.extra_number_cents;

  const startAdd = (pack: NumberPack) => {
    setConfirmation(null);
    buy.mutate(
      { code: pack.code, accept_cents: null },
      {
        onError: (err) => {
          const cents = packQuote(err);
          if (cents !== null) setConfirmation({ kind: "add", code: pack.code, size: pack.size, cents });
        },
      },
    );
  };

  const confirmAdd = () => {
    if (confirmation?.kind !== "add") return;
    const { code, size, cents } = confirmation;
    buy.mutate(
      { code, accept_cents: cents },
      {
        onSuccess: () => setConfirmation(null),
        onError: (err) => {
          const next = packQuote(err);
          if (next !== null) setConfirmation({ kind: "add", code, size, cents: next });
        },
      },
    );
  };

  const confirmRemove = () => {
    if (confirmation?.kind !== "remove") return;
    remove.mutate(confirmation.code, { onSuccess: () => setConfirmation(null) });
  };

  const confirmText =
    confirmation === null
      ? ""
      : confirmation.kind === "add"
        ? confirmation.cents >= 0
          ? `Add a ${confirmation.size}-number pack: ${dollars(confirmation.cents)} more per ${period}, charged today for the rest of this ${period}.`
          : `Add a ${confirmation.size}-number pack: it replaces add-on numbers, so your bill drops by ${dollars(-confirmation.cents)} per ${period}.`
        : `Remove one ${confirmation.size}-number pack from your next bill. No refund for this ${period}.`;

  // A price quote is the sentence that asks for confirmation, not a failure to show.
  const buyError = buy.isError && packQuote(buy.error) === null ? buy.error : null;

  return (
    <section role="region" aria-label="Number packs" className="mt-5">
      <h3 className="text-sm font-semibold">Number packs</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Cheaper than {dollars(data.extra_number_cents)} add-on numbers once you need many. Pack numbers are used before add-ons.
      </p>
      {cheapest && extrasCents > cheapest.price_cents && (
        <p className="mt-1 text-sm text-muted-foreground">You pay {dollars(extrasCents)}/mo for {extras} add-on numbers. A {cheapest.size}-number pack is {dollars(cheapest.price_cents)}/mo.</p>
      )}
      <ul className="mt-3 space-y-2">
        {shown.map(p => (
          <li key={p.code} className="flex flex-wrap items-center gap-2 rounded-xl border border-[hsl(var(--cx-line))] p-3 text-sm">
            <span className="font-semibold">{p.size} numbers</span>
            <span className="text-muted-foreground">·</span>
            <span>{dollars(p.price_cents)}/mo</span>
            {p.list_price_cents > p.price_cents && <s className="text-muted-foreground">{dollars(p.list_price_cents)}/mo</s>}
            <span className="text-muted-foreground">·</span>
            <span>{dollars(p.per_number_cents)} each</span>
            {p.owned > 0 && <span className="text-muted-foreground">You have {p.owned}</span>}
            <span className="ml-auto flex gap-2">
              {p.available && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  aria-label={`Add ${p.size}-number pack`}
                  disabled={buy.isPending}
                  onClick={() => startAdd(p)}
                >
                  Add
                </Button>
              )}
              {p.owned > 0 && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  aria-label={`Remove ${p.size}-number pack`}
                  disabled={remove.isPending}
                  onClick={() => setConfirmation({ kind: "remove", code: p.code, size: p.size })}
                >
                  Remove
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>

      {confirmation && (
        <div role="region" aria-label="Confirm number pack" className="mt-3 space-y-2 rounded-xl border border-[hsl(var(--cx-line))] p-3">
          <p className="text-sm">{confirmText}</p>
          <div className="flex flex-wrap gap-2">
            {confirmation.kind === "add" ? (
              <Button type="button" size="sm" className="rounded-full" disabled={buy.isPending} onClick={confirmAdd}>
                {buy.isPending ? "Adding…" : "Add pack"}
              </Button>
            ) : (
              <Button type="button" size="sm" className="rounded-full" disabled={remove.isPending} onClick={confirmRemove}>
                {remove.isPending ? "Removing…" : "Remove pack"}
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" className="rounded-full" onClick={() => setConfirmation(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {buyError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(buyError)}</p>}
      {remove.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(remove.error)}</p>}
    </section>
  );
}
