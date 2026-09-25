import { useState } from "react";
import { Button, Card, CardHeader, Input, MutationStatus, Spinner } from "@/components/ui/primitives";
import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { getErrorMessage } from "@/api/spend";
import {
  bundleQuote,
  formatCredits,
  formatUnitPrice,
  useBundleCheckout,
  useBundles,
  type BundleKind,
  type BundlesInfo,
} from "@/api/billing";

const KIND_COPY: Record<BundleKind, { label: string; noun: string }> = {
  sms: { label: "Text messages (SMS)", noun: "texts" },
  mms: { label: "Picture messages (MMS)", noun: "picture messages" },
};

const MAX_QTY = 500;

function clampQty(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(MAX_QTY, Math.max(1, Math.round(value)));
}

function BundleRow({ info, kind, canPay }: { info: BundlesInfo; kind: BundleKind; canPay: boolean }) {
  const { api } = useAuth();
  const checkout = useBundleCheckout(api);
  // Kept as TEXT, like BalanceCard's dollar fields, and clamped only where a number is
  // actually needed (the quote, the buy request, the stepper buttons) - clamping the input's
  // own value on every keystroke would fight typing: clearing the field to "" would
  // immediately snap back to "1" before the next digit landed, turning "clear, type 5" into
  // "15".
  const [qtyText, setQtyText] = useState("1");
  const qty = clampQty(Number(qtyText));

  const copy = KIND_COPY[kind];
  const kindInfo = info.kinds[kind];
  const quote = bundleQuote(info, kind, qty);
  const discountPct = info.volume_discount_bps / 100;
  const belowVolumeMin = kindInfo.volume_discount && qty < info.volume_min_qty;
  const disabled = !canPay || checkout.isPending;

  function handleBuy() {
    if (!canPay || checkout.isPending) return;
    checkout.mutate(
      { kind, qty },
      {
        onSuccess: (result) => {
          window.location.assign(result.checkout_url);
        },
      },
    );
  }

  return (
    <div className="space-y-2 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <h3 className="text-sm font-semibold">{copy.label}</h3>

      <p className="text-sm text-muted-foreground">
        {kindInfo.units.toLocaleString("en-US")} left
      </p>

      <p className="text-sm">
        {kindInfo.units_per_bundle.toLocaleString("en-US")} {copy.noun} for{" "}
        {formatCredits(kindInfo.list_micros)}
        {kindInfo.volume_discount ? (
          <>
            {" "}
            — buy {info.volume_min_qty} or more at once and save {discountPct}%
          </>
        ) : null}
      </p>

      <p className="text-xs text-muted-foreground">
        Without a bundle: {formatUnitPrice(kindInfo.pay_as_you_go_micros)} each
      </p>

      <div className="mt-2 flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={`Decrease ${copy.label} quantity`}
          disabled={disabled || qty <= 1}
          onClick={() => setQtyText(String(clampQty(qty - 1)))}
        >
          -
        </Button>
        <Input
          aria-label={`${copy.label} bundle quantity`}
          type="number"
          min={1}
          max={MAX_QTY}
          value={qtyText}
          disabled={disabled}
          onChange={(event) => setQtyText(event.target.value)}
          onBlur={() => setQtyText(String(qty))}
          className="w-20 text-center"
        />
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={`Increase ${copy.label} quantity`}
          disabled={disabled || qty >= MAX_QTY}
          onClick={() => setQtyText(String(clampQty(qty + 1)))}
        >
          +
        </Button>

        <Button type="button" className="rounded-full" disabled={disabled} onClick={handleBuy}>
          Buy
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        {qty.toLocaleString("en-US")} {qty === 1 ? "bundle" : "bundles"} ·{" "}
        {quote.units.toLocaleString("en-US")} {copy.noun} · {formatCredits(quote.paid)}
        {quote.discount > 0 ? ` (you save ${formatCredits(quote.discount)})` : ""}
      </p>

      {belowVolumeMin ? (
        <p className="text-xs text-muted-foreground">
          Buy {info.volume_min_qty} or more to save {discountPct}%
        </p>
      ) : null}

      <MutationStatus
        pending={checkout.isPending}
        error={checkout.error}
        pendingLabel="Starting checkout…"
      />
    </div>
  );
}

/**
 * "Message bundles": buy a block of SMS or MMS up front instead of paying per-unit. Sits
 * right under BalanceCard (see CreditsSection) - it is the same "add capacity" job, just
 * priced as a bundle rather than as dollars.
 *
 * Every number shown (bundle size, price, discount, pay-as-you-go rate) comes from
 * GET /billing/bundles, never hard-coded, so a price change on the server needs no
 * frontend deploy.
 */
export function BundlesCard() {
  const { api } = useAuth();
  const gate = useGate();
  const canPay = gate.can("org:billing");
  const bundlesQ = useBundles(api);

  if (bundlesQ.isLoading) {
    return <Spinner label="Loading bundles" />;
  }

  if (bundlesQ.isError) {
    return (
      <Card id="bundles">
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(bundlesQ.error)}</p>
          <Button type="button" variant="outline" onClick={() => void bundlesQ.refetch()}>
            Retry
          </Button>
        </div>
      </Card>
    );
  }

  const info = bundlesQ.data;
  if (info == null) return null;

  return (
    <Card id="bundles">
      <CardHeader title="Message bundles" />

      {!canPay ? (
        <p className="mt-2 text-sm text-muted-foreground">
          Only the workspace owner can buy bundles.
        </p>
      ) : null}

      <div className="mt-4 space-y-4">
        <BundleRow info={info} kind="sms" canPay={canPay} />
        <BundleRow info={info} kind="mms" canPay={canPay} />
      </div>
    </Card>
  );
}
