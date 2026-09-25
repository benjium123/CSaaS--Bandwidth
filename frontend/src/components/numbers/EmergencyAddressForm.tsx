/**
 * Shared E911 address capture. Used by the number-checkout page today and by the console's
 * number detail later, so it stays styling-neutral: UI primitives, plain labels, and
 * text-sm/text-xs classes only - no console-only CSS variables.
 */
import type { EmergencyAddress, EmergencyAddressInput } from "@/api/numbers";
import { Input } from "@/components/ui/primitives";

export type AddressDraft = {
  name: string;
  street_address: string;
  extended_address: string;
  locality: string;
  administrative_area: string;
  postal_code: string;
};

export const EMPTY_ADDRESS_DRAFT: AddressDraft = {
  name: "",
  street_address: "",
  extended_address: "",
  locality: "",
  administrative_area: "",
  postal_code: "",
};

/** Field-level problems for a draft; empty object = valid. Keys are AddressDraft keys. */
export function addressDraftProblems(
  d: AddressDraft,
): Partial<Record<keyof AddressDraft, string>> {
  const problems: Partial<Record<keyof AddressDraft, string>> = {};
  if (!d.name.trim()) problems.name = "Enter who is at this location.";
  if (!d.street_address.trim()) problems.street_address = "Enter the street address.";
  if (!d.locality.trim()) problems.locality = "Enter the city.";
  if (!/^[A-Za-z]{2}$/.test(d.administrative_area.trim())) {
    problems.administrative_area = "Use the 2-letter state, e.g. TX.";
  }
  if (!/^\d{5}(-\d{4})?$/.test(d.postal_code.trim())) {
    problems.postal_code = "Enter a 5-digit ZIP code.";
  }
  // extended_address is optional and never a problem.
  return problems;
}

export function isAddressDraftValid(d: AddressDraft): boolean {
  return Object.keys(addressDraftProblems(d)).length === 0;
}

/** Trimmed input for the API: state upper-cased, extended_address omitted when blank, country_code "US". */
export function draftToInput(d: AddressDraft): EmergencyAddressInput {
  const input: EmergencyAddressInput = {
    name: d.name.trim(),
    street_address: d.street_address.trim(),
    locality: d.locality.trim(),
    administrative_area: d.administrative_area.trim().toUpperCase(),
    postal_code: d.postal_code.trim(),
    country_code: "US",
  };
  const extended = d.extended_address.trim();
  if (extended) input.extended_address = extended;
  return input;
}

export type AddressChoice =
  | { kind: "existing"; id: string }
  | { kind: "new"; draft: AddressDraft }
  | null;

export function isAddressChoiceReady(c: AddressChoice): boolean {
  if (!c) return false;
  if (c.kind === "existing") return true;
  return isAddressDraftValid(c.draft);
}

const FIELDS: {
  key: keyof AddressDraft;
  label: string;
  maxLength?: number;
  inputMode?: "numeric";
}[] = [
  { key: "name", label: "Business or person at this location" },
  { key: "street_address", label: "Street address" },
  { key: "extended_address", label: "Suite/floor/unit (optional)" },
  { key: "locality", label: "City" },
  { key: "administrative_area", label: "State (2-letter)", maxLength: 2 },
  { key: "postal_code", label: "ZIP code", maxLength: 10, inputMode: "numeric" },
];

export function EmergencyAddressForm({
  value,
  onChange,
  disabled,
  idPrefix,
}: {
  value: AddressDraft;
  onChange: (d: AddressDraft) => void;
  disabled?: boolean;
  idPrefix: string;
}): JSX.Element {
  const problems = addressDraftProblems(value);
  return (
    <div className="space-y-3">
      {FIELDS.map((field) => {
        const id = `${idPrefix}-${field.key}`;
        const raw = value[field.key];
        // Only surface a problem once the field has content - never shout at an untouched form.
        const problem = raw.trim() ? problems[field.key] : undefined;
        return (
          <div key={field.key} className="space-y-1">
            <label htmlFor={id} className="text-sm">
              {field.label}
            </label>
            <Input
              id={id}
              value={raw}
              disabled={disabled}
              maxLength={field.maxLength}
              inputMode={field.inputMode}
              aria-invalid={problem ? true : undefined}
              onChange={(e) => onChange({ ...value, [field.key]: e.target.value })}
            />
            {problem ? <p className="text-xs text-destructive">{problem}</p> : null}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Radio list of saved addresses plus "A different address" (which reveals
 * EmergencyAddressForm). With no saved addresses it renders just the form and reports
 * { kind: "new", draft }.
 */
export function EmergencyAddressChoice({
  addresses,
  value,
  onChange,
  disabled,
  idPrefix,
}: {
  addresses: EmergencyAddress[];
  value: AddressChoice;
  onChange: (c: AddressChoice) => void;
  disabled?: boolean;
  idPrefix: string;
}): JSX.Element {
  if (addresses.length === 0) {
    return (
      <EmergencyAddressForm
        value={value?.kind === "new" ? value.draft : EMPTY_ADDRESS_DRAFT}
        onChange={(draft) => onChange({ kind: "new", draft })}
        disabled={disabled}
        idPrefix={idPrefix}
      />
    );
  }

  const radioName = `${idPrefix}-address`;

  return (
    <fieldset className="space-y-3">
      <legend className="sr-only">Saved addresses</legend>
      {addresses.map((a) => (
        <label key={a.id} className="flex items-start gap-3 text-sm">
          <input
            type="radio"
            name={radioName}
            className="mt-0.5 h-4 w-4 accent-blue-600"
            checked={value?.kind === "existing" && value.id === a.id}
            disabled={disabled}
            onChange={() => onChange({ kind: "existing", id: a.id })}
          />
          <span>{`${a.name} — ${a.label}`}</span>
        </label>
      ))}
      <label className="flex items-start gap-3 text-sm">
        <input
          type="radio"
          name={radioName}
          className="mt-0.5 h-4 w-4 accent-blue-600"
          checked={value?.kind === "new"}
          disabled={disabled}
          onChange={() => onChange({ kind: "new", draft: EMPTY_ADDRESS_DRAFT })}
        />
        <span>A different address</span>
      </label>
      {value?.kind === "new" ? (
        <div className="pt-2">
          <EmergencyAddressForm
            value={value.draft}
            onChange={(draft) => onChange({ kind: "new", draft })}
            disabled={disabled}
            idPrefix={`${idPrefix}-new`}
          />
        </div>
      ) : null}
    </fieldset>
  );
}
