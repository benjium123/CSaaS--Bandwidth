/**
 * The words the registration screens show, in one place.
 *
 * The submit copy is deliberately unglamorous: pressing Submit does NOT file anything with
 * a carrier or with TCR. The backend only validates the fields and flips the status to
 * "submitted". Filing with the carriers happens only through the paid "Register for
 * texting" flow above the panels, so copy that implies a filing has happened is a
 * compliance lie and is kept honest here rather than improvised per page.
 */

/** Human labels for the field names the server returns in `missing_for_submission`. */
export const BRAND_FIELD_LABELS: Record<string, string> = {
  name: "Business name",
  email: "Contact email",
  street: "Street",
  city: "City",
  state: "State",
  postal_code: "Postal code",
  ein: "EIN",
};

/** Human labels for the field names the server returns in `missing_for_submission`. */
export const CAMPAIGN_FIELD_LABELS: Record<string, string> = {
  name: "Campaign name",
  use_case: "Use case",
  description: "Description",
  opt_in_process: "Opt-in process",
  sample_messages: "At least one sample message",
  opt_out_message: "Opt-out message",
};

/**
 * The mapped label, or the raw field name made readable. The required set lives on the
 * server (`missing_for_submission`); these maps only translate names, so a field added
 * later still renders as words instead of as `opt_in_process`.
 */
export function fieldLabel(labels: Record<string, string>, field: string): string {
  const mapped = labels[field];
  if (mapped !== undefined && mapped !== "") return mapped;
  const words = field.replace(/_/g, " ").trim();
  if (words === "") return field;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * The industries a brand can declare, in the order the API lists them. The brand form offers
 * these as a `<Select>` so the value that goes out is always one the carrier recognises
 * instead of whatever was typed.
 */
export const BRAND_VERTICALS: readonly { value: string; label: string }[] = [
  { value: "AGRICULTURE", label: "Agriculture" },
  { value: "COMMUNICATION", label: "Communication" },
  { value: "CONSTRUCTION", label: "Construction" },
  { value: "EDUCATION", label: "Education" },
  { value: "ENERGY", label: "Energy" },
  { value: "ENTERTAINMENT", label: "Entertainment" },
  { value: "FINANCIAL", label: "Financial" },
  { value: "GAMBLING", label: "Gambling" },
  { value: "GOVERNMENT", label: "Government" },
  { value: "HEALTHCARE", label: "Healthcare" },
  { value: "HOSPITALITY", label: "Hospitality" },
  { value: "HUMAN_RESOURCES", label: "Human resources" },
  { value: "INSURANCE", label: "Insurance" },
  { value: "LEGAL", label: "Legal" },
  { value: "MANUFACTURING", label: "Manufacturing" },
  { value: "NGO", label: "Non-profit (NGO)" },
  { value: "POLITICAL", label: "Political" },
  { value: "POSTAL", label: "Postal" },
  { value: "PROFESSIONAL", label: "Professional services" },
  { value: "REAL_ESTATE", label: "Real estate" },
  { value: "RETAIL", label: "Retail" },
  { value: "TECHNOLOGY", label: "Technology" },
  { value: "TRANSPORTATION", label: "Transportation" },
];

export const SUBMIT_BRAND_LABEL = "Mark brand ready to file";
export const SUBMIT_CAMPAIGN_LABEL = "Mark campaign ready to file";

export const SUBMIT_EXPLAINER =
  "Submitting checks the details and marks the registration ready in this workspace. It does not send anything to a carrier or to TCR by itself - to file with the carriers, use Register for texting above.";

export const SUBMIT_BRAND_SUCCESS =
  "Brand marked submitted in this workspace. Nothing has been sent to a carrier yet.";

export const SUBMIT_CAMPAIGN_SUCCESS =
  "Campaign marked submitted in this workspace. Nothing has been sent to a carrier yet.";

export const CAMPAIGN_NEEDS_APPROVED_BRAND =
  "The brand must be approved before this campaign can be submitted.";

export const CARRIER_REFS_EMPTY = "No carrier registration exists yet.";

export const STATUS_DESCRIPTIONS: Record<string, string> = {
  draft: "Not submitted yet.",
  submitted:
    "Marked ready in this workspace. Use Register for texting above to file it with the carriers.",
  approved: "The carrier approved this registration.",
  rejected:
    "The carrier rejected this registration. This is final - create a new one to try again.",
};

export const TERMINAL_STATUSES = ["approved", "rejected"] as const;

export function isTerminal(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

/** The literal union matches `PillTone` in `@/components/ui/primitives` without importing
 * it - that module pulls in React components, and importing it here would create a cycle. */
export function statusTone(
  status: string,
): "neutral" | "success" | "warning" | "danger" | "info" {
  switch (status) {
    case "submitted":
      return "info";
    case "approved":
      return "success";
    case "rejected":
      return "danger";
    case "draft":
      return "neutral";
    default:
      return "neutral";
  }
}

/**
 * The sub-use-cases the texting checkout offers, in the canonical order the payload is
 * sorted into. The checkbox labels are the human names the carrier expects; the values are
 * what the API stores.
 */
export const TEXTING_SUB_USECASES: readonly { value: string; label: string }[] = [
  { value: "2FA", label: "Two-factor authentication" },
  { value: "ACCOUNT_NOTIFICATION", label: "Account notifications" },
  { value: "CUSTOMER_CARE", label: "Customer care" },
  { value: "DELIVERY_NOTIFICATION", label: "Delivery notifications" },
  { value: "FRAUD_ALERT", label: "Fraud alerts" },
  { value: "HIGHER_EDUCATION", label: "Higher education" },
  { value: "MARKETING", label: "Marketing" },
  { value: "POLLING_VOTING", label: "Polling and voting" },
  { value: "PUBLIC_SERVICE_ANNOUNCEMENT", label: "Public service announcements" },
  { value: "SECURITY_ALERT", label: "Security alerts" },
];

/**
 * The attestation questions the carrier requires, in the order they are shown. Each one is
 * a yes/no question; `key` is the payload field name and `question` is the legend.
 */
export const TEXTING_ASSERTIONS: readonly { key: string; question: string }[] = [
  { key: "subscriberOptin", question: "People must opt in before we text them" },
  { key: "subscriberOptout", question: "People can reply STOP to opt out" },
  { key: "subscriberHelp", question: "People can reply HELP for help" },
  { key: "numberPool", question: "We send from a pool of many numbers" },
  { key: "directLending", question: "We offer direct lending or loan arrangements" },
  { key: "embeddedLink", question: "Messages contain links" },
  { key: "embeddedPhone", question: "Messages contain phone numbers other than ours" },
  { key: "ageGated", question: "Content is age-gated (alcohol, firearms, etc.)" },
  { key: "autoRenewal", question: "Recurring messages auto-renew" },
];

export const TEXTING_TERMS_LABEL =
  "I agree to the carrier 10DLC terms and confirm these answers are accurate";

/**
 * Cents as a dollar string, e.g. 2550 -> "$25.50". Every price the texting card shows goes
 * through here so the numbers on screen always match the quote the server sent.
 */
export function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
