import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth, hasPermission } from "@/auth/AuthContext";
import { CAPABILITIES_QUERY_KEY } from "@/api/capabilities";
import { KYC_KEY, useKycProfile, type KycProfile, statusCopy } from "@/api/kyc";
import { COUNTRIES } from "@/lib/countries";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { PERSONAL_AGREEMENT_POINTS, VerifyBusinessPage } from "./VerifyBusinessPage";
import { JourneyShell } from "@/components/journey/JourneyShell";

type Details = { legal_name: string; country: string; phone: string; industry: string; business_description?: string; purpose: string; customer_country: string; agreement_version?: string };

function PersonalForm({ profile }: { profile: KycProfile }) {
  const { api, me, orgId } = useAuth();
  const cache = useQueryClient();
  const personal = profile.account_type === "individual";
  const stored = (profile.use_case as (typeof profile.use_case & { applicant_details?: Details }))?.applicant_details;
  const [form, setForm] = React.useState<Details>(() => ({
    legal_name: personal ? profile.business.legal_name ?? me?.full_name ?? "" : me?.full_name ?? "",
    country: personal ? profile.business.country ?? "" : "",
    phone: personal ? profile.business.business_phone ?? "" : "",
    industry: personal ? profile.use_case?.vertical ?? "" : "",
    business_description: personal ? profile.use_case?.business_description ?? "" : "",
    purpose: personal ? profile.use_case?.description ?? "" : "",
    customer_country: personal ? profile.use_case?.destination_countries?.[0] ?? "" : "",
    ...Object.fromEntries(Object.entries(stored ?? {}).filter(([, v]) => v != null)),
  }));
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
  const save = () => api.request<KycProfile>("/api/v1/kyc/application", { method: "PUT", json: { ...form, accept_personal_agreement: personal && accepted, unified_company: !personal, phone: `+${prefix}${number.replace(/\D/g, "")}` } });
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
  const complete = !!(form.legal_name.trim() && form.country && number.trim() && (!personal || (form.industry.trim() && form.business_description?.trim() && form.purpose.trim() && form.customer_country)));
  const detailsDone = !!(form.legal_name.trim() && form.country && number.trim());
  return <form className="rj-stack" style={{ marginTop: 0 }} onSubmit={e => { e.preventDefault(); void run(personal ? "submit" : "save"); }}>
    <fieldset disabled={!canEdit || busy} className="rj-card rj-in" style={{ "--i": 1 } as React.CSSProperties}>
      <legend><span className="rj-card-head" style={{ marginBottom: 0 }}><span className="rj-num" data-done={detailsDone || undefined}>1</span><span>{personal ? "Your details" : "Your personal details"}</span></span></legend>
      <label className="rj-field"><span>Legal name</span><input aria-label="Legal name" autoComplete="name" value={form.legal_name} onChange={e => update("legal_name", e.target.value)} required minLength={2} maxLength={255} /></label>
      <label className="rj-field"><span>{personal ? "Country" : "Your country of residence"}</span><select aria-label="Country" value={form.country} onChange={e => update("country", e.target.value)} required><option value="">Select country</option>{selectCountries}</select></label>
      <div className="rj-field"><span className="rj-label">{personal ? "Phone number" : "Your personal phone number"}</span><div className="rj-row">
        <select aria-label="Phone country prefix" value={dialCountry} onChange={e => { setDialCountry(e.target.value); setSaved(false); }}>{COUNTRIES.map(c => <option key={c.value} value={c.value}>{c.label} (+{c.dialCode})</option>)}</select>
        <input aria-label="Phone number" autoComplete="tel-national" type="tel" inputMode="tel" value={number} onChange={e => { setNumber(e.target.value); setSaved(false); }} required />
      </div></div>
    </fieldset>
    <section id="identity" className="rj-card rj-in" style={{ scrollMarginTop: 90, "--i": 2 } as React.CSSProperties}>
      <div className="rj-card-head"><span className="rj-num" data-done={verified || undefined}>2</span><div><h2>Verify your identity</h2><p className="rj-card-sub">A photo of your ID and a quick selfie, handled securely by Didit. About two minutes.</p></div></div>
      {verified ? <p role="status" className="rj-ok">Identity verified</p> : owner?.status === "processing" ? <p role="status" className="rj-note">Your identity check is processing. This page updates automatically.</p> : (canEdit || mustReverify) ? <button type="button" className="rj-btn" disabled={busy || (!mustReverify && (!form.legal_name.trim() || !form.country || !number.trim()))} onClick={() => void run("verify")}>{owner?.status === "pending" ? "Continue with Didit" : "Verify with Didit"}</button> : <p className="rj-note">Identity verification is not complete.</p>}
      {owner?.last_error && <p role="alert" className="rj-error" style={{ marginTop: 12 }}>{owner.last_error}</p>}
    </section>
    {personal && <><fieldset id="use_case" disabled={!canEdit || busy} className="rj-card rj-in" style={{ scrollMarginTop: 90, "--i": 3 } as React.CSSProperties}>
      <legend><span className="rj-card-head" style={{ marginBottom: 0 }}><span className="rj-num">3</span><span>How you’ll use Ringlite</span></span></legend>
      <label className="rj-field"><span>Industry</span><input aria-label="Industry" value={form.industry} onChange={e => update("industry", e.target.value)} required maxLength={64} placeholder="e.g. Real estate, Consulting" /></label>
      <label className="rj-field"><span>Describe your business. What do you do?</span><textarea aria-label="Describe your business" value={form.business_description ?? ""} onChange={e => update("business_description", e.target.value)} required maxLength={4000} rows={4} placeholder="Tell us about your work, products or services." /></label>
      <label className="rj-field"><span>What will you use calling/texting for?</span><textarea aria-label="Calling or texting purpose" value={form.purpose} onChange={e => update("purpose", e.target.value)} required maxLength={4000} rows={4} placeholder="e.g. Following up with clients about appointments." /></label>
      <label className="rj-field"><span>Country in which your customers are</span><select aria-label="Customer country" value={form.customer_country} onChange={e => update("customer_country", e.target.value)} required><option value="">Select country</option>{selectCountries}</select></label>
    </fieldset>
    <section className="rj-card rj-in" style={{ "--i": 4 } as React.CSSProperties}>
      <div className="rj-card-head"><span className="rj-num" data-done={accepted || undefined}>4</span><div><h2>Agreement</h2></div></div>
      <ul className="rj-points">{PERSONAL_AGREEMENT_POINTS.map(point => <li key={point}>{point}</li>)}</ul>
      <label className="rj-check"><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} disabled={!canEdit || busy} required />I accept the agreement</label>
    </section></>}
    {error && <p role="alert" className="rj-error">{error}</p>}
    {canEdit && <button type="submit" className="rj-btn" data-block="true" disabled={busy || !complete || (personal && (!accepted || !verified))}>{busy ? "Saving…" : personal ? "Submit for review" : "Save your details"}</button>}
    {canEdit && personal && !verified && <p className="rj-note" style={{ textAlign: "center", margin: 0 }}>Finish the identity check to submit.</p>}
    {saved && !personal && <p role="status" className="rj-ok">Your details are saved. Continue with the company application.</p>}
  </form>;
}

export function VerificationPage() {
  const { api, orgId } = useAuth();
  const query = useKycProfile(api);
  const profile = query.data;
  const copy = profile && statusCopy(profile.status, profile.account_type);
  const waiting = !!profile && ["submitted", "in_review"].includes(profile.status);
  const personal = profile?.account_type === "individual";
  return <JourneyShell step={waiting ? 1 : 0}>
    <header className="rj-in" style={{ marginTop: 34 }}>
      <span className="rj-eyebrow">Account verification</span>
      <h1 className="rj-title">{waiting ? <>We’re on <em>it.</em></> : personal ? <>Verify it’s <em>you.</em></> : <>Verify your <em>business.</em></>}</h1>
      <p className="rj-lede">{waiting ? "A person on our team reviews every application, usually within a few hours. We’ll email you the moment it’s done." : personal ? "Four short steps. Once we approve you, choose your phone numbers and start calling. Texting needs a quick carrier registration after that." : "Tell us about the company and the person running the account. Once approved you can choose numbers and start calling."}</p>
    </header>
    <div className="rj-stack">
      {query.isPending ? <Spinner label="Loading verification" /> : query.isError || !profile ? <p role="alert" className="rj-error">{mutationErrorMessage(query.error)}</p> : <>
        {copy && <section role="status" className="rj-status rj-in"><span className="rj-eyebrow">{waiting ? "Application received" : "Your application"}</span><h2>{copy.title}</h2><p>{copy.body}</p>{profile.info_request && <p>{profile.info_request}</p>}{profile.decision_reason && <p>{profile.decision_reason}</p>}</section>}
        {!waiting && (personal ? <PersonalForm key={orgId} profile={profile} /> : <VerifyBusinessPage embedded representative={<PersonalForm key={orgId} profile={profile} />} />)}
      </>}
    </div>
  </JourneyShell>;
}
