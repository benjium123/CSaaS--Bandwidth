/**
 * The words the registration screens show, in one place.
 *
 * The submit copy is deliberately unglamorous: pressing Submit does NOT file anything with
 * a carrier or with TCR. The backend only validates the fields and flips the status to
 * "submitted". Copy that implies a filing has happened is a compliance lie, so it is kept
 * honest here rather than improvised per page.
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

export const SUBMIT_BRAND_LABEL = "Mark brand ready to file";
export const SUBMIT_CAMPAIGN_LABEL = "Mark campaign ready to file";

export const SUBMIT_EXPLAINER =
  "Submitting checks the details and marks the registration ready in this workspace. It does not send anything to a carrier or to TCR - an operator files it, and the status here changes when the carrier answers.";

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
    "Marked ready in this workspace. Awaiting an operator to file it and the carrier to answer.",
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
