import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth, hasPermission } from "@/auth/AuthContext";
import { CAPABILITIES_QUERY_KEY } from "@/api/capabilities";
import { KYC_KEY, useKycProfile, type KycProfile, statusCopy } from "@/api/kyc";
import { COUNTRIES } from "@/lib/countries";
import { Button, Input, Select, Textarea, Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { PERSONAL_AGREEMENT_POINTS, VerifyBusinessPage } from "./VerifyBusinessPage";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";

type Details = { legal_name: string; country: string; phone: string; industry: string; purpose: string; customer_country: string; agreement_version?: string };

function PersonalForm({ profile }: { profile: KycProfile }) {
  const { api, me, orgId } = useAuth();
  const cache = useQueryClient();
  const personal = profile.account_type === "individual";
  const stored = (profile.use_case as (typeof profile.use_case & { applicant_details?: Details }))?.applicant_details;
  const [form, setForm] = React.useState<Details>(() => stored ?? {
    legal_name: personal ? profile.business.legal_name ?? me?.full_name ?? "" : me?.full_name ?? "",
    country: personal ? profile.business.country ?? "" : "",
    phone: personal ? profile.business.business_phone ?? "" : "",
    industry: personal ? profile.use_case?.vertical ?? "" : "",
    purpose: personal ? profile.use_case?.description ?? "" : "",
    customer_country: personal ? profile.use_case?.destination_countries?.[0] ?? "" : "",
  });
  const [dialCountry, setDialCountry] = React.useState(() => {
    const match = [...COUNTRIES].sort((a, b) => b.dialCode.length - a.dialCode.length)
      .find(c => form.phone.startsWith(`+${c.dialCode}`));
    return COUNTRIES.find(c => c.value === form.country && form.phone.startsWith(`+${c.dialCode}`))?.value ?? match?.value ?? "US";
  });
  const prefix = COUNTRIES.find(c => c.value === dialCountry)!.dialCode;
  const [number, setNumber] = React.useState(() => form.phone.replace(new RegExp(`^\\+${prefix}`), ""));
  const [accepted, setAccepted] = React.useState(personal ? profile.agreement.accepted_version === profile.agreement.current_version && !!profile.agreement.accepted_at : stored?.agreement_version === profile.agreement.current_version);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [saved, setSaved] = React.useState(false);
  const owner = profile.persons.find(p => p.is_you && p.role === "owner");
  const canEdit = hasPermission(me, orgId, "org:update") && ["draft", "needs_info"].includes(profile.status);
  const stale = !owner?.verified_at || (!!profile.next_reverification_at && new Date(owner.verified_at) < new Date(profile.next_reverification_at));
  const mustReverify = profile.status === "reverification_due" || (profile.status === "needs_info" && profile.missing.includes("id_verification")) || (personal && profile.status === "suspended" && stale);
  const verified = owner?.status === "verified" && !mustReverify;
  const update = (key: keyof Details, value: string) => { setForm(f => ({ ...f, [key]: value })); setSaved(false); };
  const refresh = async () => {
    await cache.invalidateQueries({ queryKey: KYC_KEY });
    await cache.invalidateQueries({ queryKey: CAPABILITIES_QUERY_KEY });
  };
  const save = () => api.request<KycProfile>("/api/v1/kyc/application", { method: "PUT", json: { ...form, accept_personal_agreement: accepted, phone: `+${prefix}${number.replace(/\D/g, "")}` } });
  const run = async (action: "verify" | "submit" | "save") => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const latest = canEdit ? await save() : profile;
      if (action === "verify") {
        const self = latest.persons.find(p => p.is_you && p.role === "owner");
        if (!self) throw new Error("Your identity record could not be loaded. Please try again.");
        const result = await api.request<{ url: string }>(`/api/v1/kyc/persons/${self.id}/verify`, { method: "POST", json: { return_url: `${window.location.origin}/verification#identity` } });
        window.location.assign(result.url);
      } else {
        if (personal && accepted && (profile.agreement.accepted_version !== profile.agreement.current_version || !profile.agreement.accepted_at)) {
          await api.request("/api/v1/kyc/agreement", { method: "POST", json: { version: profile.agreement.current_version, accept: true } });
        }
        if (action === "submit") await api.request("/api/v1/kyc/submit", { method: "POST" });
        setSaved(true);
      }
      await refresh();
    } catch (e) { setError(mutationErrorMessage(e)); }
    finally { setBusy(false); }
  };
  const selectCountries = COUNTRIES.map(c => <option key={c.value} value={c.value}>{c.label}</option>);
  const complete = !!(form.legal_name.trim() && form.country && number.trim() && form.industry.trim() && form.purpose.trim() && form.customer_country);
  return <form className="space-y-6" onSubmit={e => { e.preventDefault(); void run(personal ? "submit" : "save"); }}>
    <fieldset disabled={!canEdit || busy} className="space-y-4 rounded-2xl border border-[hsl(var(--cx-line))] p-5">
      <legend className="px-2 text-lg font-semibold">1. Your details</legend>
      <label className="block space-y-2"><span>Legal name</span><Input aria-label="Legal name" autoComplete="name" value={form.legal_name} onChange={e => update("legal_name", e.target.value)} required minLength={2} maxLength={255} /></label>
      <label className="block space-y-2"><span>Country</span><Select aria-label="Country" value={form.country} onChange={e => update("country", e.target.value)} required><option value="">Select country</option>{selectCountries}</Select></label>
      <div className="space-y-2"><span>Phone number</span><div className="grid gap-2 sm:grid-cols-2">
        <Select aria-label="Phone country prefix" value={dialCountry} onChange={e => { setDialCountry(e.target.value); setSaved(false); }}>{COUNTRIES.map(c => <option key={c.value} value={c.value}>{c.label} (+{c.dialCode})</option>)}</Select>
        <Input aria-label="Phone number" autoComplete="tel-national" type="tel" value={number} onChange={e => { setNumber(e.target.value); setSaved(false); }} required />
      </div></div>
    </fieldset>
    <section id="identity" className="scroll-mt-6 space-y-4 rounded-2xl border border-[hsl(var(--cx-line))] p-5">
      <h2 className="text-lg font-semibold">2. Verify your identity</h2>
      <p>Verify your ID and selfie securely with Didit.</p>
      {verified ? <p role="status">Identity verified</p> : owner?.status === "processing" ? <p role="status">Your identity check is processing. This page updates automatically.</p> : (canEdit || mustReverify) ? <Button type="button" disabled={busy || (!mustReverify && (!form.legal_name.trim() || !form.country || !number.trim()))} onClick={() => void run("verify")}>{owner?.status === "pending" ? "Continue with Didit" : "Verify with Didit"}</Button> : <p>Identity verification is not complete.</p>}
      {owner?.last_error && <p role="alert">{owner.last_error}</p>}
    </section>
    <fieldset id="use_case" disabled={!canEdit || busy} className="scroll-mt-6 space-y-4 rounded-2xl border border-[hsl(var(--cx-line))] p-5">
      <legend className="px-2 text-lg font-semibold">3. How you’ll use Ringlite</legend>
      <label className="block space-y-2"><span>Industry</span><Input aria-label="Industry" value={form.industry} onChange={e => update("industry", e.target.value)} required maxLength={64} /></label>
      <label className="block space-y-2"><span>{personal ? "What will you use calling for?" : "What will you use calling/texting for?"}</span><Textarea aria-label="Calling or texting purpose" value={form.purpose} onChange={e => update("purpose", e.target.value)} required maxLength={4000} rows={4} /></label>
      <label className="block space-y-2"><span>Country in which your customers are</span><Select aria-label="Customer country" value={form.customer_country} onChange={e => update("customer_country", e.target.value)} required><option value="">Select country</option>{selectCountries}</Select></label>
    </fieldset>
    <section className="space-y-4 rounded-2xl border border-[hsl(var(--cx-line))] p-5">
      <h2 className="text-lg font-semibold">Agreement</h2>
      <ul className="list-disc space-y-2 pl-5 text-sm">{PERSONAL_AGREEMENT_POINTS.map(point => <li key={point}>{point}</li>)}</ul>
      <label className="flex items-center gap-3"><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} disabled={!canEdit || busy} required />I accept the agreement</label>
    </section>
    {error && <p role="alert" className="text-red-600">{error}</p>}
    {canEdit && <Button type="submit" disabled={busy || !complete || !accepted || !verified}>{busy ? "Saving…" : personal ? "Submit for review" : "Save personal verification"}</Button>}
    {saved && !personal && <p role="status">Personal verification saved. Complete the company application below to submit for review.</p>}
  </form>;
}

export function VerificationPage() {
  const { theme } = useSurfaceTheme();
  const { api, orgId } = useAuth();
  const query = useKycProfile(api);
  const profile = query.data;
  const copy = profile && statusCopy(profile.status, profile.account_type);
  return <main className={`console-surface ${surfaceThemeClass(theme)} min-h-screen bg-background px-4 py-8 text-foreground`}>
    <div className="mx-auto max-w-3xl space-y-6">
      <header><p className="text-sm font-semibold">Ringlite</p><h1 className="mt-2 text-3xl font-semibold">Account verification</h1></header>
      {query.isPending ? <Spinner label="Loading verification" /> : query.isError || !profile ? <p role="alert">{mutationErrorMessage(query.error)}</p> : <>
        {profile.account_type === "individual" && <p>Individual accounts support calling only. SMS and MMS are unavailable.</p>}
        {copy && <section role="status" className="rounded-2xl border p-5"><h2 className="font-semibold">{copy.title}</h2><p>{copy.body}</p>{profile.info_request && <p>{profile.info_request}</p>}{profile.decision_reason && <p>{profile.decision_reason}</p>}</section>}
        {!["submitted", "in_review"].includes(profile.status) && <PersonalForm key={orgId} profile={profile} />}
        {profile.account_type !== "individual" && <VerifyBusinessPage />}
      </>}
    </div>
  </main>;
}
