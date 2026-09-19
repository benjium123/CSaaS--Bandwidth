import * as React from "react";
import { DocumentReview, OwnerResidence } from "@/components/kyc/OwnerResidence";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import {
  COUNTRY_OPTIONS,
  DOCUMENT_KINDS,
  ENTITY_OPTIONS,
  VERTICAL_OPTIONS,
  missingLabel,
  statusCopy,
  uploadKycDocument,
  useKycMutation,
  useKycProfile,
  type KycBusiness,
  type KycPerson,
  type KycProfile,
  type KycUseCase,
} from "@/api/kyc";
import {
  Button,
  Input,
  Pill,
  Select,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import {
  ConsoleCard,
  InitialsAvatar,
  PageHeader,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

const PERSON_STATUS: Record<KycPerson["status"], { label: string; tone: "neutral" | "success" | "warning" | "danger" | "info" }> = {
  not_started: { label: "ID check not started", tone: "neutral" },
  pending: { label: "Waiting for ID check", tone: "info" },
  processing: { label: "Checking ID", tone: "info" },
  verified: { label: "ID verified", tone: "success" },
  requires_input: { label: "ID check needs another try", tone: "warning" },
  canceled: { label: "ID check cancelled", tone: "warning" },
};

const AGREEMENT_POINTS = [
  "Everything in this application is true. Giving false information is fraud and ends the account.",
  "We only call and text people who expect to hear from us, and we follow do-not-call and consent laws.",
  "Calls and messages may be recorded and automatically reviewed to detect scams and abuse. We are responsible for telling the people we contact when the law requires it.",
  "Using the service for scams, spoofing, harassment or unsolicited robocalls leads to immediate suspension, a fixed penalty per violating call or message taken from our balance, and reports to law enforcement.",
  "Accounts cannot be sold, shared with or operated for another business.",
];

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="block space-y-[6px]">
      <span className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">{label}</span>
      {children}
      {hint ? (
        <span className="block text-[11px] leading-[1.5] text-[hsl(var(--cx-muted))]">{hint}</span>
      ) : null}
    </label>
  );
}

/**
 * One numbered section of the application.
 *
 * `id` is `kyc-step-{n}`: the onboarding stepper deep-links into this page, and the step
 * order is the server's `missing` order, so THE STEPS MUST NOT BE REORDERED OR REGROUPED.
 * This sweep only changed how they look.
 */
function StepCard({ n, title, done, children }: { n: number; title: string; done: boolean; children: React.ReactNode }) {
  return (
    <SurfaceCard id={`kyc-step-${n}`} className="scroll-mt-6 space-y-[14px]">
      <div className="flex items-center gap-[11px]">
        <span
          className={`grid h-7 w-7 flex-none place-items-center rounded-full text-[12px] font-semibold ${done ? "bg-[hsl(var(--cx-live)/0.18)] text-[hsl(var(--cx-live))]" : "bg-[hsl(var(--cx-overlay))] text-[hsl(var(--cx-muted))]"}`}
          aria-hidden="true"
        >
          {done ? "✓" : n}
        </span>
        <h2 className="text-[14px] font-semibold text-[hsl(var(--cx-text))]">{title}</h2>
      </div>
      {children}
    </SurfaceCard>
  );
}

function BusinessStep({ profile, editable }: { profile: KycProfile; editable: boolean }) {
  const { api } = useAuth();
  const [form, setForm] = React.useState<KycBusiness>(profile.business);
  const [address, setAddress] = React.useState(
    profile.business.registered_address ?? { line1: "", city: "", region: "", postal_code: "", country: profile.business.country ?? "US" },
  );
  const save = useKycMutation(api, () =>
    api.request("/api/v1/kyc/profile/business", {
      method: "PUT",
      json: {
        ...Object.fromEntries(Object.entries(form).filter(([k, v]) => k !== "registered_address" && k !== "operating_address" && v !== "" && v !== null)),
        registered_address: address.line1 ? { ...address, country: form.country ?? address.country } : undefined,
      },
    }),
  );
  const set = (key: keyof KycBusiness) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  return (
    <form
      className="grid gap-[12px] sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(undefined);
      }}
    >
      <Field label="Country of registration">
        <Select aria-label="Country of registration" value={form.country ?? ""} onChange={set("country")} disabled={!editable}>
          <option value="">Choose…</option>
          {COUNTRY_OPTIONS.filter(
            (c) => !profile.supported_countries || profile.supported_countries.includes(c.value) || c.value === form.country,
          ).map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </Select>
      </Field>
      <Field label="Business type">
        <Select aria-label="Business type" value={form.entity_type ?? ""} onChange={set("entity_type")} disabled={!editable}>
          <option value="">Choose…</option>
          {ENTITY_OPTIONS.map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </Select>
      </Field>
      <Field label="Legal business name" hint="Exactly as registered with the government">
        <Input aria-label="Legal business name" value={form.legal_name ?? ""} onChange={set("legal_name")} disabled={!editable} />
      </Field>
      <Field label="Trading name (if different)">
        <Input aria-label="Trading name" value={form.dba_name ?? ""} onChange={set("dba_name")} disabled={!editable} />
      </Field>
      <Field label="Registration number" hint="State file number (US) or Companies House number (UK)">
        <Input aria-label="Registration number" value={form.registration_number ?? ""} onChange={set("registration_number")} disabled={!editable} />
      </Field>
      <Field label="Tax ID" hint="EIN (US) or UTR / VAT number (UK)">
        <Input aria-label="Tax ID" value={form.tax_id ?? ""} onChange={set("tax_id")} disabled={!editable} />
      </Field>
      <Field label="Date the business was formed">
        <Input aria-label="Date formed" type="date" value={form.incorporation_date ?? ""} onChange={set("incorporation_date")} disabled={!editable} />
      </Field>
      <Field label="Website">
        <Input aria-label="Website" placeholder="https://" value={form.website ?? ""} onChange={set("website")} disabled={!editable} />
      </Field>
      <Field label="Business email" hint="An address on your company's own domain is best">
        <Input aria-label="Business email" type="email" value={form.business_email ?? ""} onChange={set("business_email")} disabled={!editable} />
      </Field>
      <Field label="Business phone">
        <Input aria-label="Business phone" value={form.business_phone ?? ""} onChange={set("business_phone")} disabled={!editable} />
      </Field>
      <div className="grid gap-[12px] sm:col-span-2 sm:grid-cols-2">
        <Field label="Registered address">
          <Input aria-label="Address line 1" value={address.line1} onChange={(e) => setAddress((a) => ({ ...a, line1: e.target.value }))} disabled={!editable} />
        </Field>
        <Field label="City">
          <Input aria-label="City" value={address.city} onChange={(e) => setAddress((a) => ({ ...a, city: e.target.value }))} disabled={!editable} />
        </Field>
        <Field label="State / county">
          <Input aria-label="Region" value={address.region ?? ""} onChange={(e) => setAddress((a) => ({ ...a, region: e.target.value }))} disabled={!editable} />
        </Field>
        <Field label="ZIP / postal code">
          <Input aria-label="Postal code" value={address.postal_code} onChange={(e) => setAddress((a) => ({ ...a, postal_code: e.target.value }))} disabled={!editable} />
        </Field>
      </div>
      {editable && (
        <div className="flex flex-wrap items-center gap-[12px] sm:col-span-2">
          <Button type="submit" disabled={save.isPending}>Save business details</Button>
          {save.isSuccess && <span className="text-[12.5px] text-[hsl(var(--cx-live))]">Saved</span>}
          {save.isError && <span role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(save.error)}</span>}
        </div>
      )}
    </form>
  );
}

function UseCaseStep({ profile, editable }: { profile: KycProfile; editable: boolean }) {
  const { api } = useAuth();
  const initial: KycUseCase = profile.use_case ?? {
    description: "",
    vertical: "",
    who_you_contact: "",
    list_source: "",
    monthly_calls: 0,
    monthly_texts: 0,
    destination_countries: ["US"],
    sample_script: "",
  };
  const [form, setForm] = React.useState<KycUseCase>(initial);
  const save = useKycMutation(api, () =>
    api.request("/api/v1/kyc/profile/use-case", { method: "PUT", json: form }),
  );
  const text = (key: keyof KycUseCase) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));
  const num = (key: "monthly_calls" | "monthly_texts") => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: Number(e.target.value || 0) }));

  return (
    <form
      className="grid gap-[12px]"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(undefined);
      }}
    >
      <p className="text-[11.5px] leading-[1.55] text-[hsl(var(--cx-muted))]">
        Be specific. Calls and messages that don't match what you describe here are flagged for review.
      </p>
      <Field label="What will you use calling and texting for?">
        <Textarea aria-label="What will you use calling and texting for" rows={3} value={form.description} onChange={text("description")} disabled={!editable} />
      </Field>
      <Field label="Line of business">
        <Select aria-label="Line of business" value={form.vertical} onChange={text("vertical")} disabled={!editable}>
          <option value="">Choose…</option>
          {VERTICAL_OPTIONS.map((v) => (
            <option key={v.value} value={v.value}>{v.label}</option>
          ))}
        </Select>
      </Field>
      <Field label="Who will you call or text?">
        <Textarea aria-label="Who will you call or text" rows={2} value={form.who_you_contact} onChange={text("who_you_contact")} disabled={!editable} />
      </Field>
      <Field label="Where do their phone numbers come from?" hint="For example: customers who booked with us, website sign-ups with consent">
        <Textarea aria-label="Where numbers come from" rows={2} value={form.list_source} onChange={text("list_source")} disabled={!editable} />
      </Field>
      <div className="grid gap-[12px] sm:grid-cols-3">
        <Field label="Calls per month">
          <Input aria-label="Calls per month" type="number" min={0} value={form.monthly_calls} onChange={num("monthly_calls")} disabled={!editable} />
        </Field>
        <Field label="Texts per month">
          <Input aria-label="Texts per month" type="number" min={0} value={form.monthly_texts} onChange={num("monthly_texts")} disabled={!editable} />
        </Field>
        <Field label="Countries you contact" hint="Comma separated, e.g. US, CA">
          <Input
            aria-label="Countries you contact"
            value={form.destination_countries.join(", ")}
            onChange={(e) => setForm((f) => ({ ...f, destination_countries: e.target.value.split(",").map((c) => c.trim().toUpperCase()).filter(Boolean) }))}
            disabled={!editable}
          />
        </Field>
      </div>
      <Field label="Example of a typical call or message (optional)">
        <Textarea aria-label="Example script" rows={2} value={form.sample_script ?? ""} onChange={text("sample_script")} disabled={!editable} />
      </Field>
      {(editable || profile.status === "approved") && (
        <div className="flex flex-wrap items-center gap-[12px]">
          <Button type="submit" disabled={save.isPending}>
            {profile.status === "approved" ? "Request a change" : "Save use case"}
          </Button>
          {save.isSuccess && <span className="text-[12.5px] text-[hsl(var(--cx-live))]">Saved</span>}
          {save.isError && <span role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(save.error)}</span>}
        </div>
      )}
    </form>
  );
}

function PeopleStep({ profile, editable }: { profile: KycProfile; editable: boolean }) {
  const { api } = useAuth();
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [percent, setPercent] = React.useState("100");
  const [isMe, setIsMe] = React.useState(profile.persons.length === 0);
  const [role, setRole] = React.useState<"owner" | "beneficial_owner">(profile.persons.some((p) => p.role === "owner") ? "beneficial_owner" : "owner");
  const [link, setLink] = React.useState<{ person: string; url: string } | null>(null);

  const add = useKycMutation(api, () =>
    api.request("/api/v1/kyc/persons", {
      method: "POST",
      json: { role, full_name: name, email: email || null, ownership_percent: percent ? Number(percent) : null, is_me: isMe },
    }),
  );
  const verify = useKycMutation(api, async (person: KycPerson) => {
    const res = await api.request<{ url: string }>(`/api/v1/kyc/persons/${person.id}/verify`, {
      method: "POST",
      json: { return_url: window.location.href },
    });
    // `is_you`, not `is_user`: `is_user` only says this person is linked to SOME account in
    // the workspace. Redirecting THIS browser on that would send you into another owner's
    // Stripe session - you would be photographed as them.
    if (person.is_you) {
      window.location.assign(res.url);
    } else {
      setLink({ person: person.full_name, url: res.url });
    }
  });

  return (
    <div className="space-y-[14px]">
      <p className="text-[11.5px] leading-[1.55] text-[hsl(var(--cx-muted))]">
        Add every owner of 25% or more. Each one takes a photo of a driver's license, ID card or
        passport and a selfie. We never see or store the images - our identity partner does.
      </p>
      {profile.persons.length > 0 && (
        <ul className="space-y-[8px]">
          {profile.persons.map((p) => (
            <li
              key={p.id}
              className="flex flex-wrap items-start justify-between gap-[11px] rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[12px] py-[11px]"
            >
              <div className="flex min-w-0 gap-[11px]">
                {/* A person is listed here, so they get the console's avatar. Seeded on the
                    person id, not the name, so a corrected spelling keeps the same hue. */}
                <InitialsAvatar name={p.full_name} seed={p.id} size="sm" className="mt-[2px]" />
                <div className="min-w-0 space-y-[6px]">
                  <p className="text-[13px] text-[hsl(var(--cx-text))]">
                    {p.full_name}
                    {p.ownership_percent != null ? ` · ${p.ownership_percent}%` : ""}
                    {p.is_you ? " · you" : ""}
                  </p>
                  <Pill tone={PERSON_STATUS[p.status].tone}>{PERSON_STATUS[p.status].label}</Pill>
                  {p.last_error && <p className="text-[11px] text-[hsl(var(--cx-muted))]">{p.last_error}</p>}
                  {(p.role === "owner" || p.role === "beneficial_owner") && (
                    <div className="mt-[8px]">
                      <OwnerResidence
                        person={p}
                        documents={profile.documents}
                        editable={editable && (profile.status === "draft" || profile.status === "needs_info")}
                      />
                    </div>
                  )}
                </div>
              </div>
              {p.status !== "verified" && p.status !== "processing" && (
                <Button type="button" size="sm" className="rounded-full" onClick={() => verify.mutate(p)} disabled={verify.isPending}>
                  {p.is_you ? "Verify my ID" : "Get their ID link"}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {link && (
        <div className="space-y-[8px] rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[12px]">
          <p className="text-[13px] text-[hsl(var(--cx-text))]">Send this private link to {link.person}. It works once.</p>
          <Input aria-label="ID check link" readOnly value={link.url} onFocus={(e) => e.currentTarget.select()} />
        </div>
      )}
      {verify.isError && <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(verify.error)}</p>}
      {editable && (
        <form
          className="grid gap-[12px] sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate(undefined, { onSuccess: () => { setName(""); setEmail(""); setIsMe(false); setRole("beneficial_owner"); } });
          }}
        >
          <Field label="Full legal name">
            <Input aria-label="Full legal name" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Email">
            <Input aria-label="Person email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Role">
            <Select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value as "owner" | "beneficial_owner")}>
              <option value="owner">Owner / director</option>
              <option value="beneficial_owner">Other owner (25%+)</option>
            </Select>
          </Field>
          <Field label="Ownership %">
            <Input aria-label="Ownership percent" type="number" min={0} max={100} value={percent} onChange={(e) => setPercent(e.target.value)} />
          </Field>
          <label className="flex items-center gap-[10px] text-[13px] text-[hsl(var(--cx-text))] sm:col-span-2">
            <input type="checkbox" checked={isMe} onChange={(e) => setIsMe(e.target.checked)} />
            This person is me
          </label>
          <div className="flex flex-wrap items-center gap-[12px] sm:col-span-2">
            <Button type="submit" disabled={!name.trim() || add.isPending}>Add person</Button>
            {add.isError && <span role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(add.error)}</span>}
          </div>
        </form>
      )}
    </div>
  );
}

function DocumentsStep({ profile, editable }: { profile: KycProfile; editable: boolean }) {
  const { api } = useAuth();
  const [kind, setKind] = React.useState("registration_certificate");
  const [file, setFile] = React.useState<File | null>(null);
  const upload = useKycMutation(api, () => uploadKycDocument(api, kind, file as File));
  const remove = useKycMutation(api, (id: string) => api.request(`/api/v1/kyc/documents/${id}`, { method: "DELETE" }));
  // Owners' proofs of address are shown with each owner, not here.
  const businessDocs = profile.documents.filter((d) => d.kind !== "proof_of_address");

  return (
    <div className="space-y-[12px]">
      <p className="text-[11.5px] leading-[1.55] text-[hsl(var(--cx-muted))]">
        Upload your certificate of incorporation or registration, plus your tax ID letter if you
        have it. PDF, JPG or PNG, up to 10 MB. Files are encrypted and only our compliance team can open them.
      </p>
      {businessDocs.length > 0 && (
        <ul className="space-y-[8px]">
          {businessDocs.map((d) => (
            <li
              key={d.id}
              className="flex flex-wrap items-center justify-between gap-[11px] rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[12px] py-[11px] text-[13px] text-[hsl(var(--cx-text))]"
            >
              <span className="truncate">
                {DOCUMENT_KINDS.find((k) => k.value === d.kind)?.label ?? d.kind} · {d.filename}
              </span>
              <DocumentReview doc={d} />
              {editable && (
                <Button type="button" variant="ghost" size="sm" className="rounded-full" aria-label={`Remove ${d.filename}`} onClick={() => remove.mutate(d.id)}>
                  Remove
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <div className="grid gap-[12px] sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <Field label="Document type">
            <Select aria-label="Document type" value={kind} onChange={(e) => setKind(e.target.value)}>
              {DOCUMENT_KINDS.map((k) => (
                <option key={k.value} value={k.value}>{k.label}</option>
              ))}
            </Select>
          </Field>
          <Field label="File">
            <Input aria-label="Document file" type="file" accept="application/pdf,image/jpeg,image/png" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
          <Button type="button" disabled={!file || upload.isPending} onClick={() => upload.mutate(undefined, { onSuccess: () => setFile(null) })}>
            Upload
          </Button>
        </div>
      )}
      {(upload.isError || remove.isError) && (
        <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(upload.error ?? remove.error)}</p>
      )}
    </div>
  );
}

function AgreementStep({ profile, editable }: { profile: KycProfile; editable: boolean }) {
  const { api } = useAuth();
  const [checked, setChecked] = React.useState(false);
  const accepted = profile.agreement.accepted_version === profile.agreement.current_version;
  const accept = useKycMutation(api, () =>
    api.request("/api/v1/kyc/agreement", { method: "POST", json: { version: profile.agreement.current_version, accept: true } }),
  );
  return (
    <div className="space-y-[12px]">
      <ul className="list-disc space-y-[8px] pl-5 text-[13px] leading-[1.55] text-[hsl(var(--cx-subtle))]">
        {AGREEMENT_POINTS.map((point) => (
          <li key={point}>{point}</li>
        ))}
      </ul>
      {accepted ? (
        <p className="text-[13px] text-[hsl(var(--cx-live))]">
          Accepted {profile.agreement.accepted_at ? new Date(profile.agreement.accepted_at).toLocaleString() : ""}
        </p>
      ) : editable ? (
        <div className="flex flex-wrap items-center gap-[12px]">
          <label className="flex items-center gap-[10px] text-[13px] text-[hsl(var(--cx-text))]">
            <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
            I agree, on behalf of the business
          </label>
          <Button type="button" className="rounded-full" disabled={!checked || accept.isPending} onClick={() => accept.mutate(undefined)}>
            Accept
          </Button>
          {accept.isError && <span role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(accept.error)}</span>}
        </div>
      ) : null}
    </div>
  );
}

export function VerifyBusinessPage() {
  const { api, me, orgId } = useAuth();
  const profileQ = useKycProfile(api);
  const submit = useKycMutation(api, () => api.request("/api/v1/kyc/submit", { method: "POST" }));
  const canEdit = hasPermission(me, orgId, "org:update");

  if (profileQ.isPending) return <Spinner label="Loading verification" />;
  if (profileQ.isError || !profileQ.data) {
    return <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(profileQ.error)}</p>;
  }
  const profile = profileQ.data;
  const editable = canEdit && (profile.status === "draft" || profile.status === "needs_info");
  const copy = statusCopy(profile.status);
  const missing = new Set(profile.missing);
  const businessDone = !["country", "legal_name", "entity_type", "registration_number", "registered_address", "website", "business_email", "business_phone"].some((k) => missing.has(k));

  return (
    <div className="mx-auto max-w-3xl space-y-[14px]">
      <SurfaceCard className="space-y-[12px]">
        {/* Heading level 2 is where this page already sat (it renders inside the Settings
            tab, under that page's h1); PageHeader keeps it there. */}
        <PageHeader
          headingLevel={2}
          title="Business verification"
          description="We verify every business before it can call or text. It keeps scammers off the network - and keeps your numbers from being flagged as spam."
        />
        {profile.status === "approved" ? (
          <ConsoleCard className="bg-[hsl(var(--cx-overlay))]">
            <p className="text-[13px] font-medium text-[hsl(var(--cx-live))]">Your business is verified.</p>
            {profile.next_reverification_at && (
              <p className="mt-[4px] text-[11.5px] text-[hsl(var(--cx-muted))]">
                Next annual check: {new Date(profile.next_reverification_at).toLocaleDateString()}
              </p>
            )}
          </ConsoleCard>
        ) : copy ? (
          <ConsoleCard className="bg-[hsl(var(--cx-overlay))]">
            <p className="text-[13px] font-semibold text-[hsl(var(--cx-text))]">{copy.title}</p>
            <p className="mt-[4px] text-[13px] leading-[1.55] text-[hsl(var(--cx-muted))]">{copy.body}</p>
            {profile.info_request && (
              <p className="mt-[11px] rounded-[12px] border border-[hsl(var(--cx-flag)/0.35)] bg-[hsl(var(--cx-flag)/0.12)] p-[11px] text-[12.5px] text-[hsl(var(--cx-text))]">
                <span className="font-semibold">From our reviewer: </span>
                {profile.info_request}
              </p>
            )}
            {profile.decision_reason && (
              <p className="mt-[11px] text-[12.5px] text-[hsl(var(--cx-subtle))]">{profile.decision_reason}</p>
            )}
          </ConsoleCard>
        ) : null}
      </SurfaceCard>

      <StepCard n={1} title="Your business" done={businessDone}>
        <BusinessStep profile={profile} editable={editable} />
      </StepCard>
      <StepCard n={2} title="How you'll use calling and texting" done={!profile.missing.some((m) => m.startsWith("use_case."))}>
        <UseCaseStep profile={profile} editable={editable} />
      </StepCard>
      <StepCard n={3} title="Owners, ID check and home address" done={!missing.has("owner") && !missing.has("id_verification") && !missing.has("residential_address") && !missing.has("proof_of_address")}>
        <PeopleStep profile={profile} editable={editable || profile.status === "reverification_due"} />
      </StepCard>
      <StepCard n={4} title="Business documents" done={!missing.has("documents")}>
        <DocumentsStep profile={profile} editable={editable} />
      </StepCard>
      <StepCard n={5} title="Agreement" done={!missing.has("agreement")}>
        <AgreementStep profile={profile} editable={editable} />
      </StepCard>

      {editable && (
        <SurfaceCard className="space-y-[12px]">
          {profile.missing.length > 0 ? (
            <div className="space-y-[8px]">
              <p className="text-[13px] font-semibold text-[hsl(var(--cx-text))]">
                Still needed before you can submit:
              </p>
              <ul className="list-disc space-y-[4px] pl-5 text-[12.5px] text-[hsl(var(--cx-muted))]">
                {[...new Set(profile.missing.map(missingLabel))].map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-[13px] text-[hsl(var(--cx-text))]">Everything is ready. Submit for review.</p>
          )}
          <div className="flex flex-wrap items-center gap-[12px]">
            <Button type="button" className="rounded-full" disabled={profile.missing.length > 0 || submit.isPending} onClick={() => submit.mutate(undefined)}>
              {submit.isPending ? "Submitting…" : "Submit for review"}
            </Button>
            {submit.isError && <span role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">{mutationErrorMessage(submit.error)}</span>}
          </div>
        </SurfaceCard>
      )}
    </div>
  );
}
