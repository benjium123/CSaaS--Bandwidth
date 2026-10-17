import { useState, type FormEvent, type ReactNode } from "react";

import {
  checkCampaign,
  type CampaignCheckResult,
  type RegistrationBrand,
  type RegistrationCampaign,
  type TextingCheckoutInput,
  type TextingRegistrationState,
} from "@/api/registration";
import { useAuth } from "@/auth/AuthContext";
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

/** An approved business is reused as is; only its declined campaign is filed again. */
function isReusableBrand(status: string): boolean {
  return status === "approved";
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
  quotesResubmit,
  pending,
  error,
  onSubmit,
  approvedBrandOnly = false,
}: {
  brands: RegistrationBrand[];
  campaigns: RegistrationCampaign[];
  quotes: TextingRegistrationState["quotes"];
  /** Same tiers without the brand fee, for an already approved business (older servers omit it). */
  quotesResubmit?: TextingRegistrationState["quotes"];
  pending: boolean;
  error: unknown;
  onSubmit: (input: TextingCheckoutInput) => void;
  /** Adding another campaign: only the already approved business can be picked. */
  approvedBrandOnly?: boolean;
}): JSX.Element {
  const [brandId, setBrandId] = useState("");
  const [campaignId, setCampaignId] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [mobile, setMobile] = useState("");
  const [subUsecases, setSubUsecases] = useState<string[]>([]);
  const [answers, setAnswers] = useState<Record<string, boolean | undefined>>({});
  const [terms, setTerms] = useState(false);
  const { api } = useAuth();
  const [check, setCheck] = useState<CampaignCheckResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);

  const eligibleBrands = brands.filter((brand) =>
    approvedBrandOnly
      ? isReusableBrand(brand.status)
      : isSelectable(brand.status) || isReusableBrand(brand.status),
  );
  const selectedBrand = brands.find((brand) => brand.id === brandId);
  const brandReused = selectedBrand !== undefined && isReusableBrand(selectedBrand.status);
  const isSoleProp = selectedBrand?.entity_type === "SOLE_PROPRIETOR";

  const eligibleCampaigns = campaigns.filter(
    (campaign) =>
      campaign.brand_id === brandId &&
      (isSelectable(campaign.status) || (brandReused && campaign.status === "rejected")),
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
    brandReused ||
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

  // The check is advisory: it only changes the pay button's label and what rides along.
  const hasLikelyRejection =
    check !== null && check.suggestions.some((item) => item.severity === "likely-rejection");

  const tierKey = isSoleProp ? "sole_proprietor" : "standard";
  const useResubmit = brandReused && quotesResubmit !== undefined;
  const quote = (useResubmit ? quotesResubmit : quotes)[tierKey];
  const quoteSentence =
    // Totals only: the customer sees what they pay, not how it splits between the carrier
    // fees and Ringlite's registration fee.
    `${formatCents(quote.due_today_cents)} today (includes the first ${quote.upfront_months} months). ` +
    `Then ${formatCents(quote.monthly_cents)}/month from month four. ` +
    `Not refundable once filed.`;

  function handleBrandChange(value: string) {
    setBrandId(value);
    changeCampaign("");
    setSubUsecases([]);
  }

  function changeCampaign(value: string) {
    setCampaignId(value);
    setCheck(null);
    setCheckError(null);
  }

  async function runCheck() {
    if (campaignId === "" || checking) return;
    setChecking(true);
    setCheckError(null);
    try {
      setCheck(await checkCampaign(api, campaignId));
    } catch (caught) {
      setCheck(null);
      setCheckError(
        caught instanceof Error ? caught.message : "We could not check this campaign.",
      );
    } finally {
      setChecking(false);
    }
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
      ...(check
        ? { review: { suggestions: check.suggestions, submitted_anyway: hasLikelyRejection } }
        : {}),
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
            onChange={(event) => changeCampaign(event.target.value)}
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

      {brandReused ? (
        <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
          Your business is already approved - you pay only for the campaign review.
        </p>
      ) : null}

      {isSoleProp && !brandReused ? (
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

      <div className="flex flex-col gap-[6px]">
        <div>
          <Button
            type="button"
            disabled={campaignId === "" || checking}
            onClick={() => void runCheck()}
          >
            {checking ? "Checking…" : "Check my campaign"}
          </Button>
        </div>
        {checkError ? (
          <div role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
            {checkError}
          </div>
        ) : null}
        {check ? (
          <div data-testid="campaign-check" className="flex flex-col gap-[6px]">
            {check.suggestions.length === 0 ? (
              <p className="text-[12.5px]">No problems found.</p>
            ) : (
              <ul className="flex flex-col gap-[6px]">
                {check.suggestions.map((item, index) => (
                  <li key={`${item.field}-${index}`} className="text-[12.5px]">
                    <span className="font-medium">
                      {item.severity === "likely-rejection" ? "Likely rejection" : "Improve"}
                    </span>{" "}
                    <span className="text-[hsl(var(--cx-muted))]">{item.field}</span>
                    <div>{item.why}</div>
                    {item.suggested_text ? (
                      <div className="text-[hsl(var(--cx-muted))]">
                        Suggested: {item.suggested_text}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
              {check.checks_left_today} checks left today
            </p>
          </div>
        ) : null}
        <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
          These are suggestions. You can submit anyway.
        </p>
      </div>

      <div>
        <Button type="submit" disabled={!canSubmit}>
          {hasLikelyRejection ? "Submit anyway" : "Pay and register"}
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
