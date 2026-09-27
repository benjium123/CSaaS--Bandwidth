import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { CAPABILITIES_QUERY_KEY } from "@/api/capabilities";
import { mutationErrorMessage } from "@/components/ui/primitives";
import { JourneyShell } from "@/components/journey/JourneyShell";

/** Signup: the first credit top-up (at least $5), between approval and choosing numbers. */
export const FUNDING_PRESETS_DOLLARS = [5, 10, 25, 50] as const;
const MIN_DOLLARS = 5;
const MAX_DOLLARS = 5_000;

export function AddCreditPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const returned = new URLSearchParams(window.location.search).get("topup") === "done";
  const [amount, setAmount] = useState("10");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const dollars = Number(amount);
  const valid = amount.trim() !== "" && Number.isFinite(dollars) && dollars >= MIN_DOLLARS && dollars <= MAX_DOLLARS;

  async function pay() {
    if (!valid) return;
    setPending(true);
    setError("");
    try {
      const { checkout_url } = await api.request<{ checkout_url: string }>("/api/v1/billing/topups", {
        method: "POST",
        json: { amount_micros: Math.round(dollars * 100) * 10_000, return_to: "onboarding" },
      });
      window.location.assign(checkout_url);
    } catch (err) {
      setError(mutationErrorMessage(err));
      setPending(false);
    }
  }

  return <JourneyShell step={2}>
    <header className="rj-in" style={{ marginTop: 34 }}>
      <span className="rj-eyebrow">Your application is approved</span>
      <h1 className="rj-title">Add <em>credit.</em></h1>
      <p className="rj-lede">Calls beyond your plan minutes, texts and other usage are paid from prepaid credit. Add at least ${MIN_DOLLARS} to continue; then you choose your plan and numbers.</p>
    </header>
    {returned && <section className="rj-status rj-in" style={{ marginTop: 24 }} aria-live="polite">
      <span className="rj-eyebrow">Payment</span>
      <h2>Confirming your payment…</h2>
      <p>This usually takes a few seconds. You will move on to choosing numbers automatically.</p>
      <button type="button" className="rj-btn" data-kind="quiet" onClick={() => void qc.invalidateQueries({ queryKey: CAPABILITIES_QUERY_KEY })}>Check again</button>
    </section>}
    {error && <p role="alert" className="rj-error" style={{ marginTop: 20 }}>{error}</p>}
    <section className="rj-card rj-in" style={{ marginTop: 24 }} aria-labelledby="credit-heading">
      <h2 id="credit-heading">How much credit?</h2>
      <div className="rj-plans" role="radiogroup" aria-label="Amount">{FUNDING_PRESETS_DOLLARS.map(d =>
        <label key={d} className="rj-plan" data-on={dollars === d || undefined}>
          <input type="radio" name="amount" value={d} checked={dollars === d} onChange={() => setAmount(String(d))} />
          <span className="rj-plan-price">${d}</span>
        </label>)}</div>
      <label className="rj-search" style={{ marginTop: 14 }}>
        <span className="rj-note">Other amount ($)</span>
        <input aria-label="Other amount in dollars" inputMode="decimal" value={amount} onChange={e => setAmount(e.target.value.replace(/[^\d.]/g, ""))} />
      </label>
      {!valid && amount.trim() !== "" && <p role="alert" className="rj-error">Enter an amount between ${MIN_DOLLARS} and ${MAX_DOLLARS.toLocaleString()}.</p>}
      <button type="button" className="rj-btn" data-block="true" style={{ marginTop: 16 }} disabled={!valid || pending} onClick={() => void pay()}>
        {pending ? "Opening checkout…" : valid ? `Pay $${dollars.toFixed(2)} with Stripe` : "Pay with Stripe"}
      </button>
      <p className="rj-note" style={{ marginTop: 14 }}>
        Auto-recharge is switched on with this card: when your balance drops below $5 we add $10. You can change or turn it off any time in Billing.
      </p>
      <p className="rj-note">
        Unused credit is refundable, less card processing fees. <a className="underline" href="/legal/refunds" target="_blank" rel="noreferrer">Refund policy</a>
      </p>
    </section>
  </JourneyShell>;
}
