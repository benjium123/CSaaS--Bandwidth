import { useEffect, useState, type FormEvent } from "react";
import {
  Button,
  Card,
  CardHeader,
  Input,
  MutationStatus,
  Select,
  Spinner,
} from "@/components/ui/primitives";
import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import { getErrorMessage } from "@/api/spend";
import {
  TOPUP_PRESETS_MICROS,
  WARNING_COPY,
  availableMicros,
  defaultCheckoutRedirect,
  formatCredits,
  microsToDollars,
  parseDollarsToMicros,
  useBillingSummary,
  useCreateTopup,
  usePaymentMethods,
  useUpdateAutoRecharge,
} from "@/api/billing";

function dollarsInputValue(micros: number): string {
  const dollars = microsToDollars(micros);
  return Number.isInteger(dollars) ? String(dollars) : dollars.toFixed(2);
}

// The server rejects any auto-recharge amount that is not a whole multiple of $10 with
// "Auto-recharge amount must be a multiple of $10." - this mirrors that rule client-side so
// the invalid amount is caught before the request, not after a 422.
const AUTO_RECHARGE_STEP_MICROS = 10_000_000;

function isAutoRechargeStep(micros: number): boolean {
  return micros % AUTO_RECHARGE_STEP_MICROS === 0;
}

export function BalanceCard({
  onCheckout = defaultCheckoutRedirect,
}: {
  onCheckout?: (url: string) => void;
}) {
  const { api } = useAuth();
  const gate = useGate();
  const canPay = gate.can("org:billing");

  const summaryQ = useBillingSummary(api);
  const methodsQ = usePaymentMethods(api);
  const topupMutation = useCreateTopup(api);
  const autoRechargeMutation = useUpdateAutoRecharge(api);

  const [customAmount, setCustomAmount] = useState("");
  const [autoSeeded, setAutoSeeded] = useState(false);
  const [autoEnabled, setAutoEnabled] = useState(false);
  const [thresholdText, setThresholdText] = useState("10");
  const [amountText, setAmountText] = useState("30");
  const [paymentMethodId, setPaymentMethodId] = useState<string | null>(null);

  useEffect(() => {
    if (autoSeeded || summaryQ.data == null) return;
    const auto = summaryQ.data.auto_recharge;
    if (auto) {
      setAutoEnabled(true);
      setThresholdText(dollarsInputValue(auto.threshold_micros));
      setAmountText(dollarsInputValue(auto.amount_micros));
      setPaymentMethodId(auto.payment_method_id);
    }
    setAutoSeeded(true);
  }, [autoSeeded, summaryQ.data]);

  if (summaryQ.isLoading) {
    return <Spinner label="Loading credits" />;
  }

  if (summaryQ.isError) {
    return (
      <Card>
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(summaryQ.error)}</p>
          <Button type="button" variant="outline" onClick={() => void summaryQ.refetch()}>
            Retry
          </Button>
        </div>
      </Card>
    );
  }

  const summary = summaryQ.data;
  if (summary == null) return null;

  const customMicros = parseDollarsToMicros(customAmount);
  const customInvalid = customAmount.trim() !== "" && customMicros === null;
  const thresholdMicros = parseDollarsToMicros(thresholdText);
  const rawAmountMicros = parseDollarsToMicros(amountText);
  // A parsed-but-not-a-multiple-of-$10 amount is invalid the same way a non-numeric amount
  // is: it must not reach handleAutoSave, so it is treated as null right here rather than
  // threaded through as a separate flag.
  const amountMicros =
    rawAmountMicros !== null && isAutoRechargeStep(rawAmountMicros) ? rawAmountMicros : null;
  const autoInvalid =
    (thresholdText.trim() !== "" && thresholdMicros === null) ||
    (amountText.trim() !== "" && amountMicros === null);
  const autoSaveDisabled =
    !canPay ||
    thresholdMicros === null ||
    amountMicros === null ||
    autoRechargeMutation.isPending;

  function handlePreset(presetMicros: number) {
    if (!canPay) return;
    topupMutation.mutate(
      { amount_micros: presetMicros },
      { onSuccess: (result) => onCheckout(result.checkout_url) },
    );
  }

  function handleCustomSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canPay || customMicros === null) return;
    topupMutation.mutate(
      { amount_micros: customMicros },
      { onSuccess: (result) => onCheckout(result.checkout_url) },
    );
  }

  function handleAutoSave(event: FormEvent) {
    event.preventDefault();
    if (!canPay || thresholdMicros === null || amountMicros === null) return;
    autoRechargeMutation.mutate({
      enabled: autoEnabled,
      threshold_micros: thresholdMicros,
      amount_micros: amountMicros,
      payment_method_id: paymentMethodId,
    });
  }

  return (
    <Card>
      <CardHeader title="Credits" />
      {/* The headline is what can actually be SPENT: the raw balance includes money
          already reserved for calls in flight, so showing it here tells the customer
          they have more credit than they do. The raw balance appears only in the
          on-hold sentence below, and only when there is a reserve to explain - with
          nothing on hold the two figures are equal and repeating one is just noise. */}
      <div className="mt-2 text-3xl font-semibold">{formatCredits(availableMicros(summary))}</div>

      {summary.reserved_micros > 0 ? (
        <p className="mt-1 text-sm text-muted-foreground">
          {formatCredits(summary.reserved_micros)} of your {formatCredits(summary.balance_micros)}{" "}
          balance is on hold for calls in progress.
        </p>
      ) : null}

      {summary.warning ? (
        <div role="status" className="mt-4 rounded-lg border border-border bg-muted px-3.5 py-3 text-sm">
          <p className="font-semibold">{WARNING_COPY[summary.warning].title}</p>
          <p className="mt-0.5 text-muted-foreground">{WARNING_COPY[summary.warning].body}</p>
        </div>
      ) : null}

      {!canPay ? (
        <p className="mt-4 text-sm text-muted-foreground">
          Only the workspace owner can add credits.
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {TOPUP_PRESETS_MICROS.map((presetMicros) => (
          <Button
            key={presetMicros}
            type="button"
            variant="outline"
            className="rounded-full"
            disabled={!canPay || topupMutation.isPending}
            onClick={() => handlePreset(presetMicros)}
          >
            {formatCredits(presetMicros)}
          </Button>
        ))}
      </div>

      <form className="mt-3 flex items-center gap-3" onSubmit={handleCustomSubmit}>
        <Input
          aria-label="Other amount in dollars"
          value={customAmount}
          onChange={(event) => setCustomAmount(event.target.value)}
          disabled={!canPay || topupMutation.isPending}
        />
        <Button
          type="submit"
          className="rounded-full"
          disabled={!canPay || customMicros === null || topupMutation.isPending}
        >
          Add credits
        </Button>
      </form>

      {customInvalid ? (
        <p role="alert" className="mt-2 text-sm text-destructive">
          Enter an amount in dollars, like 25.
        </p>
      ) : null}

      <MutationStatus
        pending={topupMutation.isPending}
        error={topupMutation.error}
        pendingLabel="Adding credits…"
      />

      <div className="mt-6 border-t border-border pt-4">
        <h3 className="text-sm font-semibold">Automatic top-ups</h3>

        <label className="mt-3 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            aria-label="Top up automatically when my credits run low"
            checked={autoEnabled}
            disabled={!canPay}
            onChange={(event) => setAutoEnabled(event.target.checked)}
          />
          <span>Top up automatically when my credits run low</span>
        </label>

        {/* The form (and its Save button) stays mounted when the box is unchecked: the
            only way to TURN automatic top-ups OFF is to save enabled:false, so hiding
            Save behind the checkbox would make the setting one-way. The amount fields are
            disabled instead, so they cannot be edited while the feature is off. */}
        <form className="mt-3 space-y-3" onSubmit={handleAutoSave}>
          {autoEnabled ? (
            <>
              <div className="space-y-1">
                <label htmlFor="billing-auto-threshold" className="text-xs text-muted-foreground">
                  Top up when my balance falls below
                </label>
                <Input
                  id="billing-auto-threshold"
                  value={thresholdText}
                  onChange={(event) => setThresholdText(event.target.value)}
                  disabled={!canPay || autoRechargeMutation.isPending}
                />
              </div>

              <div className="space-y-1">
                <label htmlFor="billing-auto-amount" className="text-xs text-muted-foreground">
                  Amount to add each time
                </label>
                <Input
                  id="billing-auto-amount"
                  value={amountText}
                  onChange={(event) => setAmountText(event.target.value)}
                  disabled={!canPay || autoRechargeMutation.isPending}
                />
                <p className="text-xs text-muted-foreground">
                  Charged in steps of $10. We top up when your balance drops below about one
                  day of usage (at least $5).
                  {summary.warn_threshold_micros != null
                    ? ` (currently ${formatCredits(summary.warn_threshold_micros)})`
                    : null}
                </p>
              </div>

              {Array.isArray(methodsQ.data) && methodsQ.data.length > 0 ? (
                <div className="space-y-1">
                  <label htmlFor="billing-auto-payment" className="text-xs text-muted-foreground">
                    Card to use
                  </label>
                  <Select
                    id="billing-auto-payment"
                    value={paymentMethodId ?? ""}
                    onChange={(event) =>
                      setPaymentMethodId(event.target.value === "" ? null : event.target.value)
                    }
                    disabled={!canPay || autoRechargeMutation.isPending}
                  >
                    <option value="">Select a card</option>
                    {methodsQ.data.map((method) => (
                      <option key={method.id} value={method.id}>
                        {method.brand} ending {method.last4}
                      </option>
                    ))}
                  </Select>
                </div>
              ) : null}

              {autoInvalid ? (
                <p role="alert" className="text-sm text-destructive">
                  Enter an amount in dollars, like 25.
                </p>
              ) : null}
            </>
          ) : null}

          <Button type="submit" disabled={autoSaveDisabled}>
            Save
          </Button>

          <MutationStatus
            pending={autoRechargeMutation.isPending}
            error={autoRechargeMutation.error}
            success="Saved"
          />
        </form>
      </div>
    </Card>
  );
}
