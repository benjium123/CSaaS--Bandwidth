import { useState, type FormEvent, type ReactNode } from "react";

import {
  type RegistrationBrand,
  type RegistrationCampaign,
  type TextingCheckoutInput,
  type TextingRegistrationState,
} from "@/api/registration";
import { Button, Input, MutationStatus, Select } from "@/components/ui/primitives";

import {
  TEXTING_ASSERTIONS,
  TEXTING_SUB_USECASES,
  TEXTING_TERMS_LABEL,
  formatCents,
} from "./copy";

/** The campaign use cases the standard (non sole proprietor) flow may register. */
const STANDARD_USE_CASES: readonly string[] = [
  "MIXED",
  "MARKETING",
  "CUSTOMER_CARE",
  "ACCOUNT_NOTIFICATION",
  "DELIVERY_NOTIFICATION",
  "2FA",
];

/** A brand or campaign can only be picked here while it is still editable. */
const REGISTERABLE_STATUSES: readonly string[] = ["draft", "submitted"];

const MOBILE_PATTERN = /^\+1\d{10}$/;
const SUB_USECASE_MAX = 5;

function isSelectable(status: string): boolean {
  return REGISTERABLE_STATUSES.includes(status);
}

/** One labelled control, bound with `<label htmlFor>` so every field has an accessible name. */
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

export function TextingRegistrationForm({
  brands,
  campaigns,
  quotes,
  pending,
  error,
  onSubmit,
}: {
  brands: RegistrationBrand[];
  campaigns: RegistrationCampaign[];
  quotes: TextingRegistrationState["quotes"];
  pending: boolean;
  error: unknown;
  onSubmit: (input: TextingCheckoutInput) => void;
}): JSX.Element {
  const [brandId, setBrandId] = useState("");
  const [campaignId, setCampaignId] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [mobile, setMobile] = useState("");
  const [subUsecases, setSubUsecases] = useState<string[]>([]);
  const [answers, setAnswers] = useState<Record<string, boolean | undefined>>({});
  const [terms, setTerms] = useState(false);

  const eligibleBrands = brands.filter((brand) => isSelectable(brand.status));
  const selectedBrand = brands.find((brand) => brand.id === brandId);
  const isSoleProp = selectedBrand?.entity_type === "SOLE_PROPRIETOR";

  const eligibleCampaigns = campaigns.filter(
    (campaign) => campaign.brand_id === brandId && isSelectable(campaign.status),
  );
  const selectedCampaign = campaigns.find((campaign) => campaign.id === campaignId);
  const showUseCaseAlert =
    !isSoleProp &&
    selectedCampaign !== undefined &&
    !STANDARD_USE_CASES.includes(selectedCampaign.use_case);
  const useCaseOk =
    isSoleProp ||
    (selectedCampaign !== undefined && STANDARD_USE_CASES.includes(selectedCampaign.use_case));

  // Sole proprietors send 1..5; a MIXED campaign on any other entity type needs 2..5.
  const subRequired = isSoleProp || selectedCampaign?.use_case === "MIXED";
  const subMin = isSoleProp ? 1 : 2;
  const subCountOk = !subRequired || (subUsecases.length >= subMin && subUsecases.length <= SUB_USECASE_MAX);
  const subHint = isSoleProp ? "Choose 1 to 5." : "Choose 2 to 5.";

  const solePropOk =
    !isSoleProp ||
    (firstName.trim() !== "" && lastName.trim() !== "" && MOBILE_PATTERN.test(mobile.trim()));

  const allAnswered = TEXTING_ASSERTIONS.every((assertion) => answers[assertion.key] !== undefined);

  const canSubmit =
    brandId !== "" &&
    campaignId !== "" &&
    useCaseOk &&
    solePropOk &&
    subCountOk &&
    allAnswered &&
    terms &&
    !pending;

  const quote = quotes[isSoleProp ? "sole_proprietor" : "standard"];
  const quoteSentence =
    `${formatCents(quote.due_today_cents)} today \u2014 carrier brand registration ` +
    `${formatCents(quote.brand_fee_cents)} + campaign review ${formatCents(quote.campaign_review_cents)} + ` +
    `first ${quote.upfront_months} months (${formatCents(quote.monthly_cents)}/month). ` +
    `Then ${formatCents(quote.monthly_cents)}/month from month four. ` +
    `Carrier fees are passed through at cost and are not refundable once filed.`;

  function handleBrandChange(value: string) {
    setBrandId(value);
    setCampaignId("");
    setSubUsecases([]);
  }

  function toggleSubUsecase(value: string) {
    setSubUsecases((previous) =>
      previous.includes(value) ? previous.filter((item) => item !== value) : [...previous, value],
    );
  }

  function answerAssertion(key: string, value: boolean) {
    setAnswers((previous) => ({ ...previous, [key]: value }));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;

    const assertions: Record<string, boolean> = {};
    for (const assertion of TEXTING_ASSERTIONS) {
      assertions[assertion.key] = answers[assertion.key] === true;
    }
    assertions.termsAndConditions = true;

    onSubmit({
      brand_id: brandId,
      campaign_id: campaignId,
      first_name: isSoleProp ? firstName.trim() : "",
      last_name: isSoleProp ? lastName.trim() : "",
      mobile_phone: isSoleProp ? mobile.trim() : null,
      // Canonical order, never click order; an unrequired set goes out as an empty list.
      sub_usecases: subRequired
        ? TEXTING_SUB_USECASES.filter((item) => subUsecases.includes(item.value)).map(
            (item) => item.value,
          )
        : [],
      assertions,
    });
  }

  if (eligibleBrands.length === 0) {
    return (
      <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
        Add a business profile below first.
      </p>
    );
  }

  return (
    <form
      aria-label="Register for texting"
      noValidate
      className="flex flex-col gap-[11px]"
      onSubmit={handleSubmit}
    >
      <div className="grid gap-[11px] sm:grid-cols-2">
        <Field id="texting-brand" label="Business profile">
          <Select
            id="texting-brand"
            value={brandId}
            onChange={(event) => handleBrandChange(event.target.value)}
          >
            <option value="">Select a business profile</option>
            {eligibleBrands.map((brand) => (
              <option key={brand.id} value={brand.id}>
                {brand.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field id="texting-campaign" label="Campaign">
          <Select
            id="texting-campaign"
            value={campaignId}
            onChange={(event) => setCampaignId(event.target.value)}
          >
            <option value="">Select a campaign</option>
            {eligibleCampaigns.map((campaign) => (
              <option key={campaign.id} value={campaign.id}>
                {campaign.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {isSoleProp ? (
        <>
          <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
            Sole proprietors can text from one number.
          </p>
          <div className="grid gap-[11px] sm:grid-cols-2">
            <Field id="texting-first-name" label="First name">
              <Input
                id="texting-first-name"
                value={firstName}
                onChange={(event) => setFirstName(event.target.value)}
              />
            </Field>

            <Field id="texting-last-name" label="Last name">
              <Input
                id="texting-last-name"
                value={lastName}
                onChange={(event) => setLastName(event.target.value)}
              />
            </Field>

            <Field
              id="texting-mobile"
              label="Owner's mobile"
              hint="US mobile as +1 and 10 digits, e.g. +15551234567. The carrier texts a 6-digit code to it."
            >
              <Input
                id="texting-mobile"
                type="tel"
                value={mobile}
                onChange={(event) => setMobile(event.target.value)}
              />
            </Field>
          </div>
        </>
      ) : null}

      {showUseCaseAlert ? (
        <div role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
          {"This campaign's use case can't be registered here. Choose a standard use case."}
        </div>
      ) : null}

      {subRequired ? (
        <fieldset className="flex flex-col gap-[6px] border-0 p-0">
          <legend className="text-[12.5px] font-medium text-[hsl(var(--cx-text))]">
            What will you send?
          </legend>
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">{subHint}</p>
          <div className="flex flex-col gap-[4px]">
            {TEXTING_SUB_USECASES.map((item) => {
              const checked = subUsecases.includes(item.value);
              return (
                <label key={item.value} className="flex items-center gap-2 text-[12.5px]">
                  <input
                    type="checkbox"
                    value={item.value}
                    checked={checked}
                    disabled={!checked && subUsecases.length >= SUB_USECASE_MAX}
                    onChange={() => toggleSubUsecase(item.value)}
                  />
                  {item.label}
                </label>
              );
            })}
          </div>
        </fieldset>
      ) : null}

      {TEXTING_ASSERTIONS.map((assertion) => (
        <fieldset key={assertion.key} className="flex flex-col gap-[4px] border-0 p-0">
          <legend className="text-[12.5px] font-medium text-[hsl(var(--cx-text))]">
            {assertion.question}
          </legend>
          <div className="flex items-center gap-[14px]">
            <label className="flex items-center gap-2 text-[12.5px]">
              <input
                type="radio"
                name={`texting-assert-${assertion.key}`}
                checked={answers[assertion.key] === true}
                onChange={() => answerAssertion(assertion.key, true)}
              />
              Yes
            </label>
            <label className="flex items-center gap-2 text-[12.5px]">
              <input
                type="radio"
                name={`texting-assert-${assertion.key}`}
                checked={answers[assertion.key] === false}
                onChange={() => answerAssertion(assertion.key, false)}
              />
              No
            </label>
          </div>
        </fieldset>
      ))}

      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          id="texting-terms"
          checked={terms}
          onChange={(event) => setTerms(event.target.checked)}
        />
        <label htmlFor="texting-terms" className="text-[12.5px]">
          {TEXTING_TERMS_LABEL}
        </label>
      </div>

      {selectedBrand ? (
        <p data-testid="texting-quote" className="text-[12.5px] text-[hsl(var(--cx-muted))]">
          {quoteSentence}
        </p>
      ) : null}

      <div>
        <Button type="submit" disabled={!canSubmit}>
          Pay and register
        </Button>
      </div>

      <MutationStatus
        pending={pending}
        error={error}
        pendingLabel={"Opening payment page\u2026"}
      />
    </form>
  );
}
