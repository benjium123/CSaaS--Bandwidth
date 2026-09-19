/**
 * Toll-free verification (TFV) domain.
 *
 * TFV is a SEPARATE path from 10DLC: 10DLC registers a company plus campaigns, whereas
 * TFV verifies ONE toll-free number and the messaging it carries. Nothing here touches
 * `@/api/registration` or `@/api/hooks` - only the numbers domain is reused, for the
 * e164 join the list response does not carry.
 *
 * Verified against the backend:
 *   GET  /api/v1/registration/tollfree                  -> TollFreeVerification[]
 *   POST /api/v1/registration/tollfree                  -> TollFreeVerification (201)
 *   POST /api/v1/registration/tollfree/{tfv_id}/submit  -> TollFreeVerification
 *
 * The create response (`TfvOut`) is much smaller than the create input (`TfvIn`): the
 * server echoes id/number_id/business_name/status/last_error and nothing else, so the
 * form fields are write-only and must never be read back off the row.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { PillTone } from "@/components/ui/primitives";
import type { ApiClient } from "./client";

export const TFV_PATH = "/api/v1/registration/tollfree";

export type TfvStatus = "draft" | "submitted" | "approved" | "rejected";

/** What `TfvOut` actually returns. The number's E.164 is deliberately absent - join to the
 * numbers list on `number_id` to render a phone number. */
export type TollFreeVerification = {
  id: string;
  number_id: string;
  business_name: string;
  status: TfvStatus | string;
  last_error: string | null;
};

/** Request body for create. `use_case_summary` and `opt_in_process` are typed optional
 * because that is what the schema accepts, but the backend's submit step refuses anything
 * where they are empty - the form is responsible for requiring them. */
export type TfvCreateInput = {
  number_id: string;
  business_name: string;
  use_case: string;
  use_case_summary?: string;
  opt_in_process?: string;
  opt_in_screenshot_url?: string;
  message_volume?: number;
  contact_email?: string;
};

/** Same cache key `api/hooks.ts` already reads, so a mutation here refreshes both. */
export const TFV_QUERY_KEY = ["tollfree-verifications"] as const;

/** `approved` and `rejected` are terminal: the backend raises ConflictError on a second
 * submit, so the UI must not offer one. */
export const TFV_TERMINAL_STATUSES = ["approved", "rejected"] as const;

export function isTerminalTfvStatus(status: string): boolean {
  return (TFV_TERMINAL_STATUSES as readonly string[]).includes(status);
}

export function tfvStatusTone(status: string): PillTone {
  switch (status) {
    case "approved":
      return "success";
    case "rejected":
      return "danger";
    case "submitted":
      return "info";
    case "draft":
      return "neutral";
    default:
      return "neutral";
  }
}

/** Standard toll-free use cases, values uppercase as the API expects. MIXED is the
 * server-side default and stays first. */
export const TFV_USE_CASES: readonly { value: string; label: string }[] = [
  { value: "MIXED", label: "Mixed" },
  { value: "TWO_FA", label: "Two-factor authentication" },
  { value: "ACCOUNT_NOTIFICATION", label: "Account notification" },
  { value: "CUSTOMER_CARE", label: "Customer care" },
  { value: "DELIVERY_NOTIFICATION", label: "Delivery notification" },
  { value: "FRAUD_ALERT", label: "Fraud alert" },
  { value: "HIGHER_EDUCATION", label: "Higher education" },
  { value: "MARKETING", label: "Marketing" },
  { value: "POLLING_VOTING", label: "Polling and voting" },
  { value: "PUBLIC_SERVICE_ANNOUNCEMENT", label: "Public service announcement" },
  { value: "SECURITY_ALERT", label: "Security alert" },
];

export function useTollFreeVerificationList(api: ApiClient) {
  return useQuery({
    queryKey: TFV_QUERY_KEY,
    queryFn: () => api.request<TollFreeVerification[]>(TFV_PATH),
  });
}

export function useCreateTollFreeVerification(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: TfvCreateInput) =>
      api.request<TollFreeVerification>(TFV_PATH, { method: "POST", json: input }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TFV_QUERY_KEY });
    },
  });
}

/** Takes the verification id directly. Submitting only advances the internal state machine
 * in this workspace - nothing is transmitted to a carrier from here. */
export function useSubmitTollFreeVerification(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tfvId: string) =>
      api.request<TollFreeVerification>(
        `${TFV_PATH}/${encodeURIComponent(tfvId)}/submit`,
        { method: "POST" },
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: TFV_QUERY_KEY });
    },
  });
}

/**
 * Whether a number can carry a toll-free verification at all.
 *
 * The backend sets `number_type = "tollfree"` itself for the 800/833/844/855/866/877/888
 * prefixes and rejects create with a 422 for anything else, so the picker filters on this
 * single predicate rather than re-deriving the prefix list on the client (where it would
 * drift). Pure, so it is directly testable.
 */
export function isTollFree(n: { number_type: string }): boolean {
  return n.number_type === "tollfree";
}
