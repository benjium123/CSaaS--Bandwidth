import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useAvailableNumbers, useEmergencyAddresses } from "@/api/numbers";
import { EmergencyAddressChoice, draftToInput, isAddressChoiceReady, isAddressDraftValid } from "@/components/numbers/EmergencyAddressForm";
import type { AddressChoice } from "@/components/numbers/EmergencyAddressForm";
import { Button, Input, mutationErrorMessage } from "@/components/ui/primitives";

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
      if (addressChoice?.kind === "existing") body.emergency_address_id = addressChoice.id;
      else if (addressChoice?.kind === "new" && isAddressDraftValid(addressChoice.draft)) body.emergency_address = draftToInput(addressChoice.draft);
      const result = await api.request<Purchase>("/api/v1/billing/number-checkout", { method: "POST", json: body });
      if (result.checkout_url) window.location.assign(result.checkout_url);
      else setPurchase(result);
    } catch (e) { setError(mutationErrorMessage(e)); }
    finally { setPending(false); }
  }
  return <main className="min-h-screen bg-gradient-to-br from-blue-50 via-white to-slate-100 px-6 py-12">
    <div className="mx-auto max-w-5xl">
      <p className="mb-10 text-lg font-semibold text-blue-700">Ringlite</p>
      <p className="text-xs font-semibold uppercase tracking-widest text-blue-600">Your application is approved</p>
      <h1 className="mt-3 text-4xl font-semibold tracking-tight text-slate-900">Choose your phone numbers</h1>
      <p className="mt-4 max-w-2xl text-lg leading-7 text-slate-500">A local presence for every conversation. Each number is $15/month. Select your numbers, complete payment, and open your inbox.</p>
      {error && <p role="alert" className="mt-6 rounded-xl bg-red-50 p-4 text-red-800">{error}</p>}
      {purchase && <section className="mt-8 rounded-2xl border border-blue-200 bg-white p-6"><h2 className="text-xl font-semibold">{purchase.state === "checkout" ? "Your checkout is ready" : purchase.state === "refunded" ? "Your payment was refunded" : "Setting up your phone numbers"}</h2><p className="my-4">{purchase.detail || (purchase.state === "checkout" ? "No payment has been confirmed yet. You can return to secure checkout." : "Payment received. We are connecting your numbers to your inbox.")}</p>{purchase.state === "refunded" && <Button className="bg-blue-600 text-white hover:bg-blue-700" onClick={() => window.location.assign("/choose-numbers")}>Choose numbers again</Button>}{purchase.checkout_url && <Button className="bg-blue-600 text-white hover:bg-blue-700" onClick={() => window.location.assign(purchase.checkout_url!)}>Return to checkout</Button>}{purchase.state === "checkout" && !purchase.checkout_url && <Button className="bg-blue-600 text-white hover:bg-blue-700" disabled={pending} onClick={() => void checkout()}>Retry checkout</Button>}{purchase.state === "checkout" && purchase.checkout_url && <Button className="bg-blue-600 text-white hover:bg-blue-700" disabled={pending} variant="ghost" onClick={() => void cancelCheckout()}>Choose different numbers</Button>}<p className="mt-3 text-xs text-slate-500">Purchase reference: {purchase.id}</p></section>}
      {!purchaseId && <div className="mt-10 grid gap-8 lg:grid-cols-[1fr_320px]">
        <div className="space-y-8">
        <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-xl font-semibold">Find your local number</h2>
          <form className="my-6 flex gap-3" onSubmit={e => { e.preventDefault(); setSearch(area); }}><Input aria-label="Area code" placeholder="Area code, e.g. 212" inputMode="numeric" value={area} onChange={e => setArea(e.target.value.replace(/\D/g, "").slice(0,3))} /><Button className="bg-blue-600 text-white hover:bg-blue-700" type="submit" disabled={area.length !== 3}>Search</Button></form>
          {available.isFetching && <p>Finding available numbers…</p>}
          {available.isError && <p role="alert">{mutationErrorMessage(available.error)}</p>}
          {search !== null && available.data?.length === 0 && <p>No numbers found. Try another area code.</p>}
          <div className="space-y-2">{available.data?.map(number => <label key={number.e164} className={`flex cursor-pointer items-center gap-4 rounded-xl border p-4 ${selected.includes(number.e164) ? "border-blue-500 bg-blue-50" : "border-slate-100 hover:bg-slate-50"}`}><input type="checkbox" checked={selected.includes(number.e164)} onChange={() => setSelected(old => old.includes(number.e164) ? old.filter(n => n !== number.e164) : old.length < 20 ? [...old, number.e164] : old)} className="h-5 w-5 accent-blue-600" /><span className="flex-1"><span className="block font-semibold">{number.e164}</span><span className="text-sm text-slate-500">{number.locality} {number.region}</span></span><span className="text-sm text-slate-600">$15/mo</span></label>)}</div>
        </section>
        <section aria-labelledby="e911-heading" className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 id="e911-heading" className="text-xl font-semibold">Where will these numbers be used?</h2>
          <p className="mt-2 text-sm text-slate-500">911 calls from these numbers are sent to this address.</p>
          {addresses.isPending ? <p>Loading saved addresses…</p> : addresses.isError ? <p role="alert">{mutationErrorMessage(addresses.error)}</p> : <>
            <EmergencyAddressChoice addresses={addresses.data.addresses} value={addressChoice} onChange={setAddressChoice} disabled={pending} idPrefix="checkout-e911" />
            <p className="mt-4 rounded-xl bg-slate-50 p-4 text-sm text-slate-700" data-testid="e911-notice">{addresses.data.notice}</p>
            <label className="mt-4 flex items-start gap-3 text-sm"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} className="mt-1 h-5 w-5 accent-blue-600" /><span>I understand how 911 works with these numbers</span></label>
          </>}
        </section>
        </div>
        <aside className="h-fit rounded-2xl border border-blue-100 bg-white p-6 shadow-lg shadow-blue-900/5 lg:sticky lg:top-8"><h2 className="text-xl font-semibold">Your subscription</h2><div className="my-5 space-y-2">{selected.map(n => <div className="flex justify-between text-sm" key={n}><span>{n}</span><button className="text-blue-600" onClick={() => setSelected(old => old.filter(x => x !== n))}>Remove</button></div>)}</div><div className="border-t py-5"><p className="text-sm text-slate-500">{selected.length} numbers × $15/month</p><p className="mt-2 text-3xl font-semibold">${selected.length * 15}<span className="text-base font-normal text-slate-500"> / month</span></p></div><Button className="w-full bg-blue-600 text-white hover:bg-blue-700" disabled={!selected.length || pending || !e911Ready} onClick={() => void checkout()}>{pending ? "Opening checkout…" : "Continue to payment"}</Button><p className="mt-4 text-xs leading-5 text-slate-500">Secure payment with Stripe. Number availability is confirmed when provisioned. Calling and messaging usage is billed separately.</p>{selected.length > 0 && !e911Ready && <p className="mt-3 text-xs text-slate-500">Add where these numbers will be used and confirm the 911 notice to continue.</p>}</aside>
      </div>}
    </div>
  </main>;
}
