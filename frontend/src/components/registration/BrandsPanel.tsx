import { useState, type FormEvent, type ReactNode } from "react";

import { useGate } from "@/api/capabilities";
import {
  type BrandInput,
  type RegistrationBrand,
  useCreateBrand,
  useRegistrationBrands,
  useSubmitBrand,
} from "@/api/registration";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleCard, ConsoleEmpty, SectionLabel } from "@/components/ui/consoleChrome";
import { Button, Input, MutationStatus, Pill, Select, Spinner } from "@/components/ui/primitives";

import {
  BRAND_FIELD_LABELS,
  CARRIER_REFS_EMPTY,
  STATUS_DESCRIPTIONS,
  SUBMIT_BRAND_LABEL,
  SUBMIT_BRAND_SUCCESS,
  SUBMIT_EXPLAINER,
  fieldLabel,
  isTerminal,
  statusTone,
} from "./copy";

/** The entity types the API accepts on a brand. The first one is the server's default. */
const ENTITY_TYPES = [
  "PRIVATE_PROFIT",
  "PUBLIC_PROFIT",
  "NON_PROFIT",
  "GOVERNMENT",
  "SOLE_PROPRIETOR",
] as const;

interface BrandFormFields {
  name: string;
  ein: string;
  entity_type: string;
  vertical: string;
  website: string;
  email: string;
  phone: string;
  street: string;
  city: string;
  state: string;
  postal_code: string;
  country: string;
}

const EMPTY_BRAND_FORM: BrandFormFields = {
  name: "",
  ein: "",
  entity_type: "PRIVATE_PROFIT",
  vertical: "",
  website: "",
  email: "",
  phone: "",
  street: "",
  city: "",
  state: "",
  postal_code: "",
  country: "US",
};

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
 * The create-brand form. It hands the raw field values to `useCreateBrand`, which owns the
 * trimming and the dropping of blanks through `brandCreateBody` - nothing is cleaned here.
 */
function BrandCreateForm({
  pending,
  error,
  onCreate,
}: {
  pending: boolean;
  error: unknown;
  onCreate: (input: BrandInput) => void;
}) {
  const [fields, setFields] = useState<BrandFormFields>(EMPTY_BRAND_FORM);

  function update(key: keyof BrandFormFields, value: string) {
    setFields((previous) => {
      const next: BrandFormFields = { ...previous };
      next[key] = value;
      return next;
    });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onCreate({ ...fields });
  }

  return (
    // No native validation bubbles: the submit button already refuses an empty business
    // name, and the server is the authority on what a brand needs.
    <form
      aria-label="Add brand"
      noValidate
      className="flex flex-col gap-[11px]"
      onSubmit={handleSubmit}
    >
      <div className="grid gap-[11px] sm:grid-cols-2">
        <Field id="brand-name" label="Business name">
          <Input
            id="brand-name"
            required
            value={fields.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </Field>

        <Field
          id="brand-ein"
          label="EIN"
          hint={
            fields.entity_type !== "SOLE_PROPRIETOR"
              ? "Required for every entity type except sole proprietor."
              : undefined
          }
        >
          <Input
            id="brand-ein"
            value={fields.ein}
            onChange={(event) => update("ein", event.target.value)}
          />
        </Field>

        <Field id="brand-entity-type" label="Entity type">
          <Select
            id="brand-entity-type"
            value={fields.entity_type}
            onChange={(event) => update("entity_type", event.target.value)}
          >
            {ENTITY_TYPES.map((entityType) => (
              <option key={entityType} value={entityType}>
                {entityType}
              </option>
            ))}
          </Select>
        </Field>

        <Field id="brand-vertical" label="Vertical">
          <Input
            id="brand-vertical"
            value={fields.vertical}
            onChange={(event) => update("vertical", event.target.value)}
          />
        </Field>

        <Field id="brand-website" label="Website">
          <Input
            id="brand-website"
            value={fields.website}
            onChange={(event) => update("website", event.target.value)}
          />
        </Field>

        <Field id="brand-email" label="Contact email">
          <Input
            id="brand-email"
            type="email"
            value={fields.email}
            onChange={(event) => update("email", event.target.value)}
          />
        </Field>

        <Field id="brand-phone" label="Phone">
          <Input
            id="brand-phone"
            type="tel"
            value={fields.phone}
            onChange={(event) => update("phone", event.target.value)}
          />
        </Field>

        <Field id="brand-street" label="Street">
          <Input
            id="brand-street"
            value={fields.street}
            onChange={(event) => update("street", event.target.value)}
          />
        </Field>

        <Field id="brand-city" label="City">
          <Input
            id="brand-city"
            value={fields.city}
            onChange={(event) => update("city", event.target.value)}
          />
        </Field>

        <Field id="brand-state" label="State">
          <Input
            id="brand-state"
            value={fields.state}
            onChange={(event) => update("state", event.target.value)}
          />
        </Field>

        <Field id="brand-postal-code" label="Postal code">
          <Input
            id="brand-postal-code"
            value={fields.postal_code}
            onChange={(event) => update("postal_code", event.target.value)}
          />
        </Field>

        <Field id="brand-country" label="Country">
          <Input
            id="brand-country"
            value={fields.country}
            onChange={(event) => update("country", event.target.value)}
          />
        </Field>
      </div>

      <div>
        <Button type="submit" disabled={pending || fields.name.trim() === ""}>
          Create brand
        </Button>
      </div>

      <MutationStatus pending={pending} error={error} />
    </form>
  );
}

/** One brand row: status, what is still missing, and - for a manager - the submit control. */
function BrandCard({
  brand,
  canManage,
  submitting,
  onSubmitBrand,
}: {
  brand: RegistrationBrand;
  canManage: boolean;
  submitting: boolean;
  onSubmitBrand: (brandId: string) => void;
}) {
  const carrierRefs = Object.entries(brand.carrier_refs);
  const missing = brand.missing_for_submission;
  const statusDescription = STATUS_DESCRIPTIONS[brand.status];

  return (
    <ConsoleCard className="flex flex-col gap-[9px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">
          {brand.name}
        </span>
        <Pill tone={statusTone(brand.status)}>{brand.status}</Pill>
      </div>

      {statusDescription ? (
        <p className="text-[12px] text-[hsl(var(--cx-muted))]">{statusDescription}</p>
      ) : null}

      <p className="text-[12px] text-[hsl(var(--cx-muted))]">
        Entity type: {brand.entity_type}
      </p>

      {brand.last_error ? (
        <div role="alert" className="text-[12px] text-[hsl(var(--cx-danger))]">
          Reason: {brand.last_error}
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
              <li key={field}>{fieldLabel(BRAND_FIELD_LABELS, field)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {canManage && !isTerminal(brand.status) ? (
        <div className="flex flex-col gap-[4px]">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              aria-label={`${SUBMIT_BRAND_LABEL}: ${brand.name}`}
              disabled={missing.length > 0 || submitting}
              onClick={() => onSubmitBrand(brand.id)}
            >
              {SUBMIT_BRAND_LABEL}
            </Button>
          </div>
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">{SUBMIT_EXPLAINER}</p>
        </div>
      ) : null}
    </ConsoleCard>
  );
}

export function BrandsPanel(): JSX.Element {
  const { api } = useAuth();
  const gate = useGate();
  const canManage = gate.can("compliance:manage");

  const brandsQuery = useRegistrationBrands(api);
  const createMutation = useCreateBrand(api);
  const submitMutation = useSubmitBrand(api);

  const [formOpen, setFormOpen] = useState(false);
  // `mutation.isPending` alone would disable EVERY row's submit button, so the row that is
  // actually in flight is tracked here instead.
  const [submittingId, setSubmittingId] = useState<string | null>(null);

  const brands = Array.isArray(brandsQuery.data) ? brandsQuery.data : null;

  function handleSubmitBrand(brandId: string) {
    setSubmittingId(brandId);
    submitMutation.mutate(brandId);
  }

  return (
    <div className="flex flex-col gap-[11px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionLabel>Brands</SectionLabel>
        {canManage ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setFormOpen((open) => !open)}
          >
            {formOpen ? "Cancel" : "Add brand"}
          </Button>
        ) : null}
      </div>

      {canManage && formOpen ? (
        <BrandCreateForm
          pending={createMutation.isPending}
          error={createMutation.error}
          onCreate={(input) =>
            createMutation.mutate(input, { onSuccess: () => setFormOpen(false) })
          }
        />
      ) : null}

      {brandsQuery.isLoading ? <Spinner label="Loading brands" /> : null}

      {brandsQuery.isError ? (
        <div role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
          {brandsQuery.error instanceof Error
            ? brandsQuery.error.message
            : "Could not load brands."}
        </div>
      ) : null}

      {brands !== null && brands.length === 0 ? (
        <ConsoleEmpty>No brand registered for this workspace yet.</ConsoleEmpty>
      ) : null}

      {brands?.map((brand) => (
        <BrandCard
          key={brand.id}
          brand={brand}
          canManage={canManage}
          submitting={submitMutation.isPending && submittingId === brand.id}
          onSubmitBrand={handleSubmitBrand}
        />
      ))}

      <MutationStatus
        pending={submitMutation.isPending}
        error={submitMutation.error}
        success={submitMutation.isSuccess ? SUBMIT_BRAND_SUCCESS : undefined}
      />
    </div>
  );
}
