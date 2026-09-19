import { useState, type FormEvent, type ReactNode } from "react";

import { useGate } from "@/api/capabilities";
import {
  type CampaignInput,
  type RegistrationBrand,
  type RegistrationCampaign,
  useCreateCampaign,
  useRegistrationBrands,
  useRegistrationCampaigns,
  useSubmitCampaign,
} from "@/api/registration";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleCard, ConsoleEmpty, SectionLabel } from "@/components/ui/consoleChrome";
import {
  Button,
  Input,
  MutationStatus,
  Pill,
  Select,
  Spinner,
  Textarea,
} from "@/components/ui/primitives";

import {
  CAMPAIGN_FIELD_LABELS,
  CAMPAIGN_NEEDS_APPROVED_BRAND,
  CARRIER_REFS_EMPTY,
  STATUS_DESCRIPTIONS,
  SUBMIT_CAMPAIGN_LABEL,
  SUBMIT_CAMPAIGN_SUCCESS,
  SUBMIT_EXPLAINER,
  fieldLabel,
  isTerminal,
  statusTone,
} from "./copy";

/** The use cases the API accepts on a campaign. The first one is the server's default. */
const USE_CASES = [
  "MIXED",
  "MARKETING",
  "CUSTOMER_CARE",
  "ACCOUNT_NOTIFICATION",
  "DELIVERY_NOTIFICATION",
  "2FA",
] as const;

interface CampaignFormFields {
  brand_id: string;
  name: string;
  use_case: string;
  description: string;
  opt_in_process: string;
  sample_messages: string;
  help_message: string;
  opt_out_message: string;
}

const EMPTY_CAMPAIGN_FORM: CampaignFormFields = {
  brand_id: "",
  name: "",
  use_case: "MIXED",
  description: "",
  opt_in_process: "",
  sample_messages: "",
  help_message: "",
  opt_out_message: "",
};

/** "1 number assigned" reads better than "1 numbers assigned"; everything else is plural. */
function numberCountLabel(count: number): string {
  return count === 1 ? "1 number assigned" : `${count} numbers assigned`;
}

/**
 * One labelled control. The label is a real `<label htmlFor>` bound to the control's id so
 * every field has an accessible name.
 */
function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[11.5px] font-medium text-[hsl(var(--cx-muted))]">
        {label}
      </label>
      {children}
      {hint ? <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">{hint}</p> : null}
    </div>
  );
}

/**
 * The create-campaign form. The raw field values go to `useCreateCampaign`, which owns the
 * per-line trimming and blank-line dropping through `campaignCreateBody` - nothing is
 * filtered here, and the sample-messages textarea is split on newlines only.
 */
function CampaignCreateForm({
  brands,
  pending,
  error,
  onCreate,
}: {
  brands: RegistrationBrand[];
  pending: boolean;
  error: unknown;
  onCreate: (input: CampaignInput) => void;
}) {
  const [fields, setFields] = useState<CampaignFormFields>(EMPTY_CAMPAIGN_FORM);

  function update(key: keyof CampaignFormFields, value: string) {
    setFields((previous) => {
      const next: CampaignFormFields = { ...previous };
      next[key] = value;
      return next;
    });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onCreate({
      brand_id: fields.brand_id,
      name: fields.name,
      use_case: fields.use_case,
      description: fields.description,
      opt_in_process: fields.opt_in_process,
      sample_messages: fields.sample_messages.split("\n"),
      help_message: fields.help_message,
      opt_out_message: fields.opt_out_message,
    });
  }

  const noBrand = fields.brand_id === "";
  const noName = fields.name.trim() === "";

  return (
    // No native validation bubbles: the submit button already refuses an unselected brand
    // or an empty name, and the server is the authority on what a campaign needs.
    <form
      aria-label="Add campaign"
      noValidate
      className="flex flex-col gap-[11px]"
      onSubmit={handleSubmit}
    >
      <div className="grid gap-[11px] sm:grid-cols-2">
        <Field id="campaign-brand" label="Brand">
          <Select
            id="campaign-brand"
            required
            value={fields.brand_id}
            onChange={(event) => update("brand_id", event.target.value)}
          >
            <option value="">Select a brand</option>
            {brands.map((brand) => (
              <option key={brand.id} value={brand.id}>
                {brand.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field id="campaign-name" label="Campaign name">
          <Input
            id="campaign-name"
            required
            value={fields.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </Field>

        <Field id="campaign-use-case" label="Use case">
          <Select
            id="campaign-use-case"
            value={fields.use_case}
            onChange={(event) => update("use_case", event.target.value)}
          >
            {USE_CASES.map((useCase) => (
              <option key={useCase} value={useCase}>
                {useCase}
              </option>
            ))}
          </Select>
        </Field>

        <Field id="campaign-description" label="Description">
          <Textarea
            id="campaign-description"
            value={fields.description}
            onChange={(event) => update("description", event.target.value)}
          />
        </Field>

        <Field id="campaign-opt-in-process" label="Opt-in process">
          <Textarea
            id="campaign-opt-in-process"
            value={fields.opt_in_process}
            onChange={(event) => update("opt_in_process", event.target.value)}
          />
        </Field>

        <Field id="campaign-sample-messages" label="Sample messages" hint="One message per line.">
          <Textarea
            id="campaign-sample-messages"
            value={fields.sample_messages}
            onChange={(event) => update("sample_messages", event.target.value)}
          />
        </Field>

        <Field id="campaign-help-message" label="HELP message">
          <Textarea
            id="campaign-help-message"
            value={fields.help_message}
            onChange={(event) => update("help_message", event.target.value)}
          />
        </Field>

        <Field id="campaign-opt-out-message" label="Opt-out message">
          <Textarea
            id="campaign-opt-out-message"
            value={fields.opt_out_message}
            onChange={(event) => update("opt_out_message", event.target.value)}
          />
        </Field>
      </div>

      <div>
        <Button type="submit" disabled={pending || noBrand || noName}>
          Create campaign
        </Button>
      </div>

      <MutationStatus pending={pending} error={error} />
    </form>
  );
}

/** One campaign row, including whether its parent brand is approved yet. */
function CampaignCard({
  campaign,
  parentBrand,
  canManage,
  submitting,
  onSubmitCampaign,
}: {
  campaign: RegistrationCampaign;
  parentBrand: RegistrationBrand | undefined;
  canManage: boolean;
  submitting: boolean;
  onSubmitCampaign: (campaignId: string) => void;
}) {
  const carrierRefs = Object.entries(campaign.carrier_refs);
  const missing = campaign.missing_for_submission;
  const statusDescription = STATUS_DESCRIPTIONS[campaign.status];
  const terminal = isTerminal(campaign.status);
  // Fail closed: a brand that is not in the list counts as not approved, because the server
  // refuses a campaign whose brand is not `approved`.
  const brandApproved = parentBrand != null && parentBrand.status === "approved";

  return (
    <ConsoleCard className="flex flex-col gap-[9px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">
          {campaign.name}
        </span>
        <Pill tone={statusTone(campaign.status)}>{campaign.status}</Pill>
      </div>

      {statusDescription ? (
        <p className="text-[12px] text-[hsl(var(--cx-muted))]">{statusDescription}</p>
      ) : null}

      <p className="text-[12px] text-[hsl(var(--cx-muted))]">
        Brand: {parentBrand ? parentBrand.name : "unknown"}
      </p>

      <p className="text-[12px] text-[hsl(var(--cx-muted))]">Use case: {campaign.use_case}</p>

      <p className="text-[12px] text-[hsl(var(--cx-muted))]">
        {numberCountLabel(campaign.number_count)}
      </p>

      {campaign.last_error ? (
        <div role="alert" className="text-[12px] text-[hsl(var(--cx-danger))]">
          Reason: {campaign.last_error}
        </div>
      ) : null}

      <div className="flex flex-col gap-[3px]">
        <SectionLabel>Carrier registrations</SectionLabel>
        {carrierRefs.length > 0 ? (
          <ul className="flex flex-col gap-[2px] text-[12px] text-[hsl(var(--cx-muted))]">
            {carrierRefs.map(([carrier, id]) => (
              <li key={carrier}>
                {carrier}: {String(id)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-[hsl(var(--cx-muted))]">{CARRIER_REFS_EMPTY}</p>
        )}
      </div>

      {missing.length > 0 ? (
        <div className="flex flex-col gap-[3px]">
          <SectionLabel>Still needed before submitting</SectionLabel>
          {/* The list comes from the server; there is no required-field list in this file. */}
          <ul className="list-disc pl-[18px] text-[12px] text-[hsl(var(--cx-muted))]">
            {missing.map((field) => (
              <li key={field}>{fieldLabel(CAMPAIGN_FIELD_LABELS, field)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Only a `draft` row still has submitting as its next step. A `submitted` row whose
          brand was later rejected is not going to be submitted again, so telling the reader
          to get the brand approved first would describe an action they cannot take. */}
      {!brandApproved && campaign.status === "draft" ? (
        <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
          {CAMPAIGN_NEEDS_APPROVED_BRAND}
        </p>
      ) : null}

      {canManage && !terminal ? (
        <div className="flex flex-col gap-[4px]">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              aria-label={`${SUBMIT_CAMPAIGN_LABEL}: ${campaign.name}`}
              disabled={!brandApproved || missing.length > 0 || submitting}
              onClick={() => onSubmitCampaign(campaign.id)}
            >
              {SUBMIT_CAMPAIGN_LABEL}
            </Button>
          </div>
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">{SUBMIT_EXPLAINER}</p>
        </div>
      ) : null}
    </ConsoleCard>
  );
}

export function CampaignsPanel(): JSX.Element {
  const { api } = useAuth();
  const gate = useGate();
  const canManage = gate.can("compliance:manage");

  const campaignsQuery = useRegistrationCampaigns(api);
  // Same query key as BrandsPanel, so this reads the cache rather than asking again.
  const brandsQuery = useRegistrationBrands(api);
  const createMutation = useCreateCampaign(api);
  const submitMutation = useSubmitCampaign(api);

  const [formOpen, setFormOpen] = useState(false);
  // Tracked by id so only the clicked row shows pending - see BrandsPanel.
  const [submittingId, setSubmittingId] = useState<string | null>(null);

  const brands = Array.isArray(brandsQuery.data) ? brandsQuery.data : [];
  const campaigns = Array.isArray(campaignsQuery.data) ? campaignsQuery.data : null;

  function handleSubmitCampaign(campaignId: string) {
    setSubmittingId(campaignId);
    submitMutation.mutate(campaignId);
  }

  return (
    <div className="flex flex-col gap-[11px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionLabel>Campaigns</SectionLabel>
        {canManage ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setFormOpen((open) => !open)}
          >
            {formOpen ? "Cancel" : "Add campaign"}
          </Button>
        ) : null}
      </div>

      {/* `brands` is [] until the brands query answers, so an empty list cannot be read as
          "this workspace has no brand": a workspace that HAS one would be told to go
          register one for as long as the request takes. Loading and error are therefore
          handled before the empty case, and neither falls through to it. */}
      {canManage && formOpen ? (
        brandsQuery.isLoading ? (
          <Spinner label="Loading brands" />
        ) : brandsQuery.isError ? (
          <div role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
            Could not load brands, so a campaign cannot be created right now.
          </div>
        ) : brands.length === 0 ? (
          <p className="text-[12px] text-[hsl(var(--cx-muted))]">
            Register a brand first - a campaign belongs to a brand.
          </p>
        ) : (
          <CampaignCreateForm
            brands={brands}
            pending={createMutation.isPending}
            error={createMutation.error}
            onCreate={(input) =>
              createMutation.mutate(input, { onSuccess: () => setFormOpen(false) })
            }
          />
        )
      ) : null}

      {campaignsQuery.isLoading ? <Spinner label="Loading campaigns" /> : null}

      {campaignsQuery.isError ? (
        <div role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
          {campaignsQuery.error instanceof Error
            ? campaignsQuery.error.message
            : "Could not load campaigns."}
        </div>
      ) : null}

      {campaigns !== null && campaigns.length === 0 ? (
        <ConsoleEmpty>No campaigns registered yet.</ConsoleEmpty>
      ) : null}

      {campaigns?.map((campaign) => (
        <CampaignCard
          key={campaign.id}
          campaign={campaign}
          parentBrand={brands.find((brand) => brand.id === campaign.brand_id)}
          canManage={canManage}
          submitting={submitMutation.isPending && submittingId === campaign.id}
          onSubmitCampaign={handleSubmitCampaign}
        />
      ))}

      <MutationStatus
        pending={submitMutation.isPending}
        error={submitMutation.error}
        success={submitMutation.isSuccess ? SUBMIT_CAMPAIGN_SUCCESS : undefined}
      />
    </div>
  );
}
