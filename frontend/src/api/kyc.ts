import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";

/** P41 business verification - customer side. */

export type KycStatus =
  | "draft"
  | "submitted"
  | "in_review"
  | "needs_info"
  | "approved"
  | "rejected"
  | "suspended"
  | "reverification_due";

export type Address = {
  line1: string;
  line2?: string | null;
  city: string;
  region?: string | null;
  postal_code: string;
  country: string;
};

export type KycBusiness = {
  country: string | null;
  legal_name: string | null;
  dba_name: string | null;
  entity_type: string | null;
  registration_number: string | null;
  tax_id: string | null;
  incorporation_date: string | null;
  registered_address: Address | null;
  operating_address: Address | null;
  website: string | null;
  business_email: string | null;
  business_phone: string | null;
};

export type KycUseCase = {
  description: string;
  vertical: string;
  who_you_contact: string;
  list_source: string;
  monthly_calls: number;
  monthly_texts: number;
  destination_countries: string[];
  sample_script?: string | null;
};

export type KycPerson = {
  id: string;
  role: "owner" | "beneficial_owner" | "admin" | "billing";
  full_name: string;
  email: string | null;
  ownership_percent: number | null;
  is_user: boolean;
  status: "not_started" | "pending" | "processing" | "verified" | "requires_input" | "canceled";
  verified_name: string | null;
  document_country: string | null;
  verified_at: string | null;
  last_error: string | null;
  /** P43: where the owner lives now. */
  residential_address?: Address | null;
};

export type KycDocument = {
  id: string;
  kind: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  uploaded_at: string | null;
  /** P43: the owner a proof of address belongs to. */
  person_id?: string | null;
  /** P43: the automatic AI review - "reviewing" until it has read the document. */
  review_status?: "reviewing" | "pass" | "warn" | "fail";
  review_message?: string | null;
};

export type KycProfile = {
  status: KycStatus;
  /** P43: countries a business can verify from (server KYC_COUNTRIES). */
  supported_countries?: string[];
  business: KycBusiness;
  use_case: KycUseCase | null;
  use_case_pending: KycUseCase | null;
  persons: KycPerson[];
  documents: KycDocument[];
  checks: Record<string, { result: string; summary: string }>;
  agreement: { current_version: string; accepted_version: string | null; accepted_at: string | null };
  missing: string[];
  info_request: string | null;
  submitted_at: string | null;
  decided_at: string | null;
  decision_reason: string | null;
  limits: Record<string, number> | null;
  deposit_required_cents: number | null;
  next_reverification_at: string | null;
};

export const KYC_KEY = ["kyc", "profile"];

export const COUNTRY_OPTIONS = [
  { value: "US", label: "United States" },
  { value: "CA", label: "Canada" },
  { value: "GB", label: "United Kingdom" },
];

export const ENTITY_OPTIONS = [
  { value: "llc", label: "LLC" },
  { value: "corporation", label: "Corporation / Ltd" },
  { value: "partnership", label: "Partnership" },
  { value: "sole_proprietor", label: "Sole proprietor" },
  { value: "nonprofit", label: "Non-profit" },
  { value: "government", label: "Government" },
  { value: "other", label: "Other" },
];

export const DOCUMENT_KINDS = [
  { value: "registration_certificate", label: "Certificate of incorporation / registration" },
  { value: "tax_id_letter", label: "Tax ID letter (IRS EIN letter, CRA BN, HMRC)" },
  { value: "articles", label: "Articles / operating agreement" },
  { value: "other", label: "Other" },
];

export const VERTICAL_OPTIONS = [
  { value: "home_services", label: "Home services" },
  { value: "healthcare", label: "Healthcare / clinics" },
  { value: "real_estate", label: "Real estate" },
  { value: "retail", label: "Retail / e-commerce" },
  { value: "hospitality", label: "Hospitality / restaurants" },
  { value: "professional_services", label: "Professional services" },
  { value: "education", label: "Education" },
  { value: "software", label: "Software / SaaS support" },
  { value: "nonprofit", label: "Non-profit" },
  { value: "insurance_leads", label: "Insurance" },
  { value: "loans", label: "Lending / loans" },
  { value: "debt_collection", label: "Debt collection" },
  { value: "lead_generation", label: "Lead generation" },
  { value: "other", label: "Other" },
];

/** Plain-language names for what is still missing before submitting. */
export const MISSING_LABELS: Record<string, string> = {
  country: "Country",
  legal_name: "Legal business name",
  entity_type: "Business type",
  registration_number: "Registration number",
  registered_address: "Registered address",
  website: "Website",
  business_email: "Business email",
  business_phone: "Business phone",
  owner: "An owner",
  id_verification: "ID + selfie check for every owner",
  documents: "At least one business document",
  residential_address: "Each owner's current home address",
  proof_of_address: "A recent proof of address for each owner",
  agreement: "Accept the agreement",
};

export function missingLabel(key: string): string {
  if (key.startsWith("use_case.")) return "How you will use calling and texting";
  return MISSING_LABELS[key] ?? key;
}

export function useKycProfile(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: KYC_KEY,
    queryFn: () => api.request<KycProfile>("/api/v1/kyc/profile"),
    enabled,
    staleTime: 30_000,
  });
}

export function useKycMutation<TVars>(
  _api: ApiClient,
  fn: (vars: TVars) => Promise<unknown>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KYC_KEY }),
  });
}

export async function uploadKycDocument(
  api: ApiClient,
  kind: string,
  file: File,
  personId?: string,
) {
  const form = new FormData();
  form.append("kind", kind);
  form.append("file", file);
  if (personId) form.append("person_id", personId);
  return api.request<KycDocument>("/api/v1/kyc/documents", { method: "POST", body: form });
}

export function statusCopy(status: KycStatus): { title: string; body: string } | null {
  switch (status) {
    case "draft":
      return {
        title: "Verify your business to start calling and texting",
        body: "Tell us about your business, upload a registration document and confirm your ID. Most reviews finish within one business day.",
      };
    case "submitted":
    case "in_review":
      return {
        title: "Your business is being reviewed",
        body: "Calling and texting unlock as soon as a reviewer approves it. We'll email you.",
      };
    case "needs_info":
      return {
        title: "We need a little more information",
        body: "Open business verification to see what the reviewer asked for.",
      };
    case "rejected":
      return {
        title: "Your business could not be verified",
        body: "Calling and texting are not available for this workspace. Contact support if you think this is a mistake.",
      };
    case "suspended":
      return {
        title: "This account is suspended",
        body: "Calling, texting and number orders are paused following a compliance review. Contact support.",
      };
    case "reverification_due":
      return {
        title: "Annual re-verification is due",
        body: "Each owner needs to repeat the quick ID and selfie check to keep calling and texting.",
      };
    default:
      return null;
  }
}
