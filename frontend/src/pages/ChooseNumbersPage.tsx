import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useAvailableNumbers, useEmergencyAddresses } from "@/api/numbers";
import { EmergencyAddressChoice, draftToInput, isAddressChoiceReady, isAddressDraftValid } from "@/components/numbers/EmergencyAddressForm";
import type { AddressChoice } from "@/components/numbers/EmergencyAddressForm";
import { mutationErrorMessage } from "@/components/ui/primitives";
import { JourneyShell } from "@/components/journey/JourneyShell";
import { dollars, planSaving, useWorkspacePlan } from "@/api/plan";
import type { PlanCode } from "@/api/plan";

type Purchase = { id: string; state: string; checkout_url?: string; detail?: string; numbers: { e164: string; state: string }[] };

/** A path on this site. "//x" and "/\x" are both read by browsers as another host. */
export const isLocalPath = (path: string) => path.startsWith("/") && !/^\/[/\\]/.test(path);

export function ChooseNumbersPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const [area, setArea] = useState("");
  const [search, setSearch] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [purchase, setPurchase] = useState<Purchase | null>(null);
  const [addressChoice, setAddressChoice] = useState<AddressChoice>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const addresses = useEmergencyAddresses(api);
  const workspacePlan = useWorkspacePlan(api);
  const [planCode, setPlanCode] = useState<PlanCode>("solo");
  const current = workspacePlan.data?.plan ?? null;
  const catalog = workspacePlan.data?.catalog ?? [];
  const chosen = catalog.find(p => p.code === planCode);
  const numberCents = workspacePlan.data?.extra_number_cents ?? 500;
  // Numbers the plan still includes for free; beyond them each is a $5/month add-on.
  const freeSlots = current
    ? Math.max((workspacePlan.data?.numbers.limit ?? 0) - (workspacePlan.data?.numbers.in_use ?? 0), 0)
    : chosen?.numbers ?? 0;
  const paidNumbers = Math.max(selected.length - freeSlots, 0);
  const increaseCents = paidNumbers * numberCents;
  const monthlyCents = (current ? current.monthly_total_cents : chosen?.price_cents ?? 0) + increaseCents;
  const e911Ready = addresses.isSuccess && acknowledged && isAddressChoiceReady(addressChoice);
  const purchaseId = new URLSearchParams(window.location.search).get("purchase");
  const requestedNext = new URLSearchParams(window.location.search).get("next");
  if (requestedNext && /^\/(?!\/)/.test(requestedNext)) {
    try { sessionStorage.setItem("ringlite.afterNumbers", requestedNext); } catch { /* storage may be blocked */ }
  }
  const available = useAvailableNumbers(api, { carrier: "telnyx", area_code: search ?? "", limit: 20 }, search !== null);
  useEffect(() => {
    if (!purchaseId) {
      void api.request<Purchase | null>("/api/v1/billing/number-purchases/current").then(current => {
        if (current) window.location.replace(`/choose-numbers?purchase=${current.id}`);
      }).catch(e => setError(mutationErrorMessage(e)));
      return;
    }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function check() {
      try {
        const result = await api.request<Purchase>(`/api/v1/billing/number-purchases/${purchaseId}/complete`, { method: "POST" });
        if (stopped) return;
        setPurchase(result);
        if (result.state === "complete") {
          await qc.invalidateQueries({ queryKey: ["numbers"] });
          await qc.invalidateQueries({ queryKey: ["me", "capabilities"] });
          let next = "/inbox";
          try {
            next = sessionStorage.getItem("ringlite.afterNumbers") || next;
            sessionStorage.removeItem("ringlite.afterNumbers");
          } catch { /* storage may be blocked */ }
          window.location.assign(/^\/(?!\/)/.test(next) ? next : "/inbox");
        } else if (["paid", "provisioning", "activating"].includes(result.state)) timer = setTimeout(() => void check(), 5000);
      } catch (e) { if (!stopped) setError(mutationErrorMessage(e)); }
    }
    void check();
    return () => { stopped = true; clearTimeout(timer); };
  }, [api, purchaseId, qc]);
  async function cancelCheckout() {
    if (!purchase) return;
    setPending(true);
    try {
      await api.request(`/api/v1/billing/number-purchases/${purchase.id}/cancel`, { method: "POST" });
      window.location.assign("/choose-numbers");
    } catch (e) { setError(mutationErrorMessage(e)); }
    finally { setPending(false); }
  }
  async function checkout() {
    setPending(true); setError("");
    try {
      // acknowledge_e911 mirrors the real checkbox: retrying checkout must never claim an
      // acknowledgment the user did not give - the backend answers with its own 422.
      const body: Record<string, unknown> = { numbers: purchase?.numbers.map(n => n.e164) ?? selected, acknowledge_e911: acknowledged };
      // First purchase: the plan comes with it. On a plan: echo the add-on price shown.
      if (current) body.accept_charge_cents = increaseCents;
      else body.plan_code = planCode;
      if (addressChoice?.kind === "existing") body.emergency_address_id = addressChoice.id;
      else if (addressChoice?.kind === "new" && isAddressDraftValid(addressChoice.draft)) body.emergency_address = draftToInput(addressChoice.draft);
      const result = await api.request<Purchase>("/api/v1/billing/number-checkout", { method: "POST", json: body });
      if (result.checkout_url) window.location.assign(result.checkout_url);
      else window.location.assign(`/choose-numbers?purchase=${result.id}`);
    } catch (e) { setError(mutationErrorMessage(e)); }
    finally { setPending(false); }
  }
  return <JourneyShell step={2} wide>
    <header className="rj-in" style={{ marginTop: 34 }}>
      <span className="rj-eyebrow">{current ? `Your ${current.name} plan` : "Your application is approved"}</span>
      <h1 className="rj-title">{current ? <>Add <em>numbers.</em></> : <>Choose your <em>plan.</em></>}</h1>
      <p className="rj-lede">{current
        ? `Your plan has ${freeSlots} free number${freeSlots === 1 ? "" : "s"} left. Numbers beyond that are ${dollars(numberCents)} a month each.`
        : "Every plan comes with users and phone numbers, and 200 call minutes a month for each user. Pick a plan and your numbers, pay securely with Stripe, and your inbox opens."}</p>
    </header>
    {error && <p role="alert" className="rj-error" style={{ marginTop: 20 }}>{error}</p>}
    {purchase && <section className="rj-status rj-in" style={{ marginTop: 24 }}>
      <span className="rj-eyebrow">Purchase</span>
      <h2>{purchase.state === "checkout" ? "Your checkout is ready" : purchase.state === "refunded" ? "Your payment was refunded" : "Setting up your phone numbers"}</h2>
      <p>{purchase.detail || (purchase.state === "checkout" ? "No payment has been confirmed yet. You can return to secure checkout." : "Payment received. We are connecting your numbers to your inbox.")}</p>
      <div className="flex flex-wrap gap-2" style={{ marginTop: 16 }}>
        {purchase.state === "refunded" && <button type="button" className="rj-btn" onClick={() => window.location.assign("/choose-numbers")}>Choose numbers again</button>}
        {purchase.checkout_url && <button type="button" className="rj-btn" onClick={() => window.location.assign(purchase.checkout_url!)}>Return to checkout</button>}
        {purchase.state === "checkout" && !purchase.checkout_url && <button type="button" className="rj-btn" disabled={pending} onClick={() => void checkout()}>Retry checkout</button>}
        {purchase.state === "checkout" && purchase.checkout_url && <button type="button" className="rj-btn" data-kind="quiet" disabled={pending} onClick={() => void cancelCheckout()}>Choose different numbers</button>}
      </div>
      <p className="rj-note" style={{ marginTop: 12 }}>Purchase reference: {purchase.id}</p>
    </section>}
    {!purchaseId && <div className="rj-choose">
      <div className="rj-stack" style={{ marginTop: 0 }}>
        {!current && <section className="rj-card rj-in" aria-labelledby="plan-heading" style={{ "--i": 0 } as React.CSSProperties}>
          <div className="rj-card-head"><span className="rj-num" data-done>1</span><div><h2 id="plan-heading">Choose your plan</h2><p className="rj-card-sub">Add users ({dollars(catalog.find(p => p.code === planCode)?.extra_user_cents ?? workspacePlan.data?.extra_user_cents ?? 1500)}/month) and numbers ({dollars(numberCents)}/month) any time.</p></div></div>
          {workspacePlan.isPending ? <p className="rj-note">Loading plans…</p> : workspacePlan.isError ? <p role="alert" className="rj-error">{mutationErrorMessage(workspacePlan.error)}</p> :
            <div className="rj-plans" role="radiogroup" aria-label="Plan">{catalog.map(p => {
              const saving = planSaving(p, workspacePlan.data?.extra_user_cents, numberCents);
              return <label key={p.code} className="rj-plan" data-on={p.code === planCode || undefined}>
                <input type="radio" name="plan" value={p.code} checked={p.code === planCode} onChange={() => setPlanCode(p.code)} />
                <span className="rj-plan-name">{p.name}{saving > 0 && <em>Save {dollars(saving)}</em>}</span>
                <span className="rj-plan-price">{dollars(p.price_cents)}<small>/month</small></span>
                <span className="rj-plan-facts">{p.users} user{p.users > 1 ? "s" : ""} · {p.numbers} number{p.numbers > 1 ? "s" : ""}<br />{p.minutes.toLocaleString()} call minutes/month</span>
              </label>;
            })}</div>}
        </section>}
        <section className="rj-card rj-in" style={{ "--i": 1 } as React.CSSProperties}>
          <div className="rj-card-head"><span className="rj-num" data-done={selected.length > 0 || undefined}>{current ? 1 : 2}</span><div><h2>Find your local number</h2><p className="rj-card-sub">Search by area code. Pick as many as you need.</p></div></div>
          <form className="rj-search" onSubmit={e => { e.preventDefault(); setSearch(area); }}>
            <input aria-label="Area code" placeholder="Area code, e.g. 212" inputMode="numeric" value={area} onChange={e => setArea(e.target.value.replace(/\D/g, "").slice(0, 3))} />
            <button className="rj-btn" type="submit" disabled={area.length !== 3}>Search</button>
          </form>
          {available.isFetching && <p className="rj-note">Finding available numbers…</p>}
          {available.isError && <p role="alert" className="rj-error">{mutationErrorMessage(available.error)}</p>}
          {search !== null && available.data?.length === 0 && <p className="rj-note">No numbers found. Try another area code.</p>}
          <div className="rj-numbers">{available.data?.map(number => <label key={number.e164} className="rj-number" data-on={selected.includes(number.e164) || undefined}>
            <input type="checkbox" checked={selected.includes(number.e164)} onChange={() => setSelected(old => old.includes(number.e164) ? old.filter(n => n !== number.e164) : old.length < 20 ? [...old, number.e164] : old)} />
            <span className="rj-number-main"><b>{number.e164}</b><small>{number.locality} {number.region}</small></span>
            <span className="rj-number-price">{(selected.includes(number.e164) ? selected.indexOf(number.e164) : selected.length) < freeSlots ? "Included" : `+${dollars(numberCents)}/mo`}</span>
          </label>)}</div>
        </section>
        <section aria-labelledby="e911-heading" className="rj-card rj-in" style={{ "--i": 2 } as React.CSSProperties}>
          <div className="rj-card-head"><span className="rj-num" data-done={e911Ready || undefined}>{current ? 2 : 3}</span><div><h2 id="e911-heading">Where will these numbers be used?</h2><p className="rj-card-sub">911 calls from these numbers are sent to this address.</p></div></div>
          {addresses.isPending ? <p className="rj-note">Loading saved addresses…</p> : addresses.isError ? <p role="alert" className="rj-error">{mutationErrorMessage(addresses.error)}</p> : <>
            <EmergencyAddressChoice addresses={addresses.data.addresses} value={addressChoice} onChange={setAddressChoice} disabled={pending} idPrefix="checkout-e911" />
            <p className="rj-notice" data-testid="e911-notice">{addresses.data.notice}</p>
            <label className="rj-check" style={{ marginTop: 14 }}><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>I understand how 911 works with these numbers</span></label>
          </>}
        </section>
      </div>
      <aside className="rj-card rj-summary rj-in" style={{ "--i": 3 } as React.CSSProperties}>
        <h2>Your subscription</h2>
        <div className="rj-summary-list">
          {current ? <div><span>{current.name} plan (current)</span><span>{dollars(current.monthly_total_cents)}</span></div>
            : chosen && <div><span>{chosen.name}: {chosen.users} user{chosen.users > 1 ? "s" : ""} + {chosen.numbers} number{chosen.numbers > 1 ? "s" : ""}</span><span>{dollars(chosen.price_cents)}</span></div>}
          {selected.map((n, i) => <div key={n}><span>{n}{i < freeSlots ? " · included" : ` · ${dollars(numberCents)}`}</span><button type="button" className="rj-ghost" onClick={() => setSelected(old => old.filter(x => x !== n))}>Remove</button></div>)}
        </div>
        <div className="rj-total"><p className="rj-note" style={{ margin: 0 }}>{current ? (increaseCents ? `Adds ${dollars(increaseCents)}/month, charged now (prorated)` : "Included in your plan, no extra charge") : paidNumbers ? `Includes ${paidNumbers} extra number${paidNumbers > 1 ? "s" : ""} at ${dollars(numberCents)}` : "Monthly total"}</p><p className="rj-total-amount">{dollars(monthlyCents)}<span> / month</span></p></div>
        <button type="button" className="rj-btn" data-block="true" disabled={!selected.length || pending || !e911Ready || workspacePlan.isPending} onClick={() => void checkout()}>{pending ? (current ? "Adding numbers…" : "Opening checkout…") : current ? (increaseCents ? `Pay ${dollars(increaseCents)}/month and add` : "Add numbers") : "Continue to payment"}</button>
        <p className="rj-note" style={{ marginTop: 14 }}>{current ? "Charged to the card on your plan." : "Secure payment with Stripe."} Number availability is confirmed when provisioned. Calls beyond your plan minutes, and texts, use prepaid credit.</p>
        {selected.length > 0 && !e911Ready && <p className="rj-note">Add where these numbers will be used and confirm the 911 notice to continue.</p>}
      </aside>
    </div>}
  </JourneyShell>;
}
