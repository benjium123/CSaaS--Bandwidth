import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

export const REGISTRATION_BRANDS_PATH = "/api/v1/registration/brands";
export const REGISTRATION_CAMPAIGNS_PATH = "/api/v1/registration/campaigns";

export type RegistrationStatus = "draft" | "submitted" | "approved" | "rejected";

/** POST body for creating a brand. Matches BrandIn. */
export interface BrandInput {
  name: string; // required, 1..127
  ein?: string | null; // max 32
  entity_type?: string; // server default "PRIVATE_PROFIT"
  vertical?: string | null;
  website?: string | null;
  email?: string | null;
  phone?: string | null;
  street?: string | null;
  city?: string | null;
  state?: string | null;
  postal_code?: string | null;
  country?: string; // server default "US"
}

/** GET/POST response. Matches BrandOut. */
export interface RegistrationBrand {
  id: string;
  name: string;
  entity_type: string;
  status: string;
  carrier_refs: Record<string, unknown>;
  last_error: string | null;
  /** Server-computed list of field names that still block submission. */
  missing_for_submission: string[];
}

/** POST body for creating a campaign. Matches CampaignIn. */
export interface CampaignInput {
  brand_id: string; // required
  name: string; // required, 1..127
  use_case?: string; // server default "MIXED"
  description?: string | null;
  opt_in_process?: string | null;
  sample_messages?: string[]; // server default []
  help_message?: string | null;
  opt_out_message?: string | null;
  privacy_policy_url?: string | null;
  terms_url?: string | null;
  /** Comma separated, e.g. "STOP,UNSUBSCRIBE". */
  opt_in_keywords?: string | null;
  opt_out_keywords?: string | null;
  help_keywords?: string | null;
  opt_in_message?: string | null;
  /** A link to a screenshot or the sign-up page. */
  opt_in_proof_url?: string | null;
  expected_monthly_messages?: number | null;
}

/** PATCH body for a draft or declined campaign. Matches CampaignPatchIn (no brand_id). */
export type CampaignPatch = Partial<Omit<CampaignInput, "brand_id">>;

/** GET/POST response. Matches the registration CampaignOut. */
export interface RegistrationCampaign {
  id: string;
  brand_id: string;
  name: string;
  use_case: string;
  status: string;
  carrier_refs: Record<string, unknown>;
  last_error: string | null;
  number_count: number;
  missing_for_submission: string[];
  description?: string | null;
  opt_in_process?: string | null;
  sample_messages?: string[];
  help_message?: string | null;
  opt_out_message?: string | null;
  privacy_policy_url?: string | null;
  terms_url?: string | null;
  opt_in_keywords?: string | null;
  opt_out_keywords?: string | null;
  help_keywords?: string | null;
  opt_in_message?: string | null;
  opt_in_proof_url?: string | null;
  expected_monthly_messages?: number | null;
}

/**
 * The POST body for a brand, with the payload hygiene the API expects.
 *
 * Every optional text field is trimmed and OMITTED when it comes out blank. Sending `""`
 * where the model wants `None` stores a row that looks filled in when it is not - and an
 * untouched input on the create form would otherwise arrive as exactly that. `name`,
 * `entity_type` and `country` are kept whenever the caller supplies them: the first is
 * required, and the other two are non-nullable columns the form has already defaulted.
 */
export function brandCreateBody(input: BrandInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};

  body.name = input.name.trim();
  if (typeof input.entity_type === "string") body.entity_type = input.entity_type.trim();
  if (typeof input.country === "string") body.country = input.country.trim();

  const optional: [string, string | null | undefined][] = [
    ["ein", input.ein],
    ["vertical", input.vertical],
    ["website", input.website],
    ["email", input.email],
    ["phone", input.phone],
    ["street", input.street],
    ["city", input.city],
    ["state", input.state],
    ["postal_code", input.postal_code],
  ];
  for (const [key, value] of optional) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed === "") continue;
    body[key] = trimmed;
  }

  return body;
}

/**
 * The POST body for a campaign, cleaned the same way as a brand's.
 *
 * `brand_id` and `name` always go out (required), so a blank one reaches the server and
 * comes back as a 422 the form can show instead of being dropped here. `sample_messages`
 * keeps its key even when the cleaned list is empty - an empty list is a deliberate answer
 * ("no samples yet"), not the same thing as an untouched form.
 */
export function campaignCreateBody(input: CampaignInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};

  body.brand_id = input.brand_id.trim();
  body.name = input.name.trim();

  const optional: [string, string | null | undefined][] = [
    ["use_case", input.use_case],
    ["description", input.description],
    ["opt_in_process", input.opt_in_process],
    ["help_message", input.help_message],
    ["opt_out_message", input.opt_out_message],
    ["privacy_policy_url", input.privacy_policy_url],
    ["terms_url", input.terms_url],
    ["opt_in_keywords", input.opt_in_keywords],
    ["opt_out_keywords", input.opt_out_keywords],
    ["help_keywords", input.help_keywords],
    ["opt_in_message", input.opt_in_message],
    ["opt_in_proof_url", input.opt_in_proof_url],
  ];
  for (const [key, value] of optional) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed === "") continue;
    body[key] = trimmed;
  }

  if (Array.isArray(input.sample_messages)) {
    body.sample_messages = input.sample_messages
      .map((message) => message.trim())
      .filter((message) => message !== "");
  }

  if (typeof input.expected_monthly_messages === "number") {
    body.expected_monthly_messages = input.expected_monthly_messages;
  }

  return body;
}

/**
 * The PATCH body: only the keys present on `patch`. A blank text field goes out as `null`
 * so an edit can clear a value; `name`, `use_case` and `sample_messages` are never null.
 */
export function campaignPatchBody(patch: CampaignPatch): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    if (typeof value === "string") {
      const trimmed = value.trim();
      body[key] = trimmed === "" && key !== "name" && key !== "use_case" ? null : trimmed;
    } else if (Array.isArray(value)) {
      body[key] = value.map((message) => message.trim()).filter((message) => message !== "");
    } else {
      body[key] = value;
    }
  }
  return body;
}

function brandSubmitPath(brandId: string): string {
  return `${REGISTRATION_BRANDS_PATH}/${encodeURIComponent(brandId)}/submit`;
}

function campaignSubmitPath(campaignId: string): string {
  return `${REGISTRATION_CAMPAIGNS_PATH}/${encodeURIComponent(campaignId)}/submit`;
}

export async function fetchRegistrationBrands(api: ApiClient): Promise<RegistrationBrand[]> {
  return api.request<RegistrationBrand[]>(REGISTRATION_BRANDS_PATH);
}

export async function fetchRegistrationCampaigns(
  api: ApiClient,
): Promise<RegistrationCampaign[]> {
  return api.request<RegistrationCampaign[]>(REGISTRATION_CAMPAIGNS_PATH);
}

export async function createBrand(
  api: ApiClient,
  input: BrandInput,
): Promise<RegistrationBrand> {
  return api.request<RegistrationBrand>(REGISTRATION_BRANDS_PATH, {
    method: "POST",
    json: brandCreateBody(input),
  });
}

export async function submitBrand(api: ApiClient, brandId: string): Promise<RegistrationBrand> {
  // No body: the endpoint validates the stored row and flips its status, nothing more.
  return api.request<RegistrationBrand>(brandSubmitPath(brandId), { method: "POST" });
}

export async function createCampaign(
  api: ApiClient,
  input: CampaignInput,
): Promise<RegistrationCampaign> {
  return api.request<RegistrationCampaign>(REGISTRATION_CAMPAIGNS_PATH, {
    method: "POST",
    json: campaignCreateBody(input),
  });
}

function campaignPath(campaignId: string): string {
  return `${REGISTRATION_CAMPAIGNS_PATH}/${encodeURIComponent(campaignId)}`;
}

/** Edit a draft or declined campaign. Anything else is a 409 from the server. */
export async function updateCampaign(
  api: ApiClient,
  campaignId: string,
  patch: CampaignPatch,
): Promise<RegistrationCampaign> {
  return api.request<RegistrationCampaign>(campaignPath(campaignId), {
    method: "PATCH",
    json: campaignPatchBody(patch),
  });
}

export async function submitCampaign(
  api: ApiClient,
  campaignId: string,
): Promise<RegistrationCampaign> {
  // No body: the endpoint validates the stored row and flips its status, nothing more.
  return api.request<RegistrationCampaign>(campaignSubmitPath(campaignId), { method: "POST" });
}

export const REGISTRATION_BRANDS_KEY = ["registration", "brands"] as const;
export const REGISTRATION_CAMPAIGNS_KEY = ["registration", "campaigns"] as const;

export function useRegistrationBrands(api: ApiClient) {
  return useQuery({
    queryKey: REGISTRATION_BRANDS_KEY,
    queryFn: () => fetchRegistrationBrands(api),
    retry: false,
  });
}

export function useRegistrationCampaigns(api: ApiClient) {
  return useQuery({
    queryKey: REGISTRATION_CAMPAIGNS_KEY,
    queryFn: () => fetchRegistrationCampaigns(api),
    retry: false,
  });
}

export function useCreateBrand(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: BrandInput) => createBrand(api, input),
    onSuccess: () => {
      // A new brand changes which campaign rows are submittable, so both lists refresh.
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useSubmitBrand(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (brandId: string) => submitBrand(api, brandId),
    onSuccess: () => {
      // A brand reaching `approved` is what unlocks its campaigns, and this POST is the only
      // thing that moves its status at all, so both lists must refresh.
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useCreateCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: CampaignInput) => createCampaign(api, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useUpdateCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: CampaignPatch }) =>
      updateCampaign(api, id, patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useSubmitCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (campaignId: string) => submitCampaign(api, campaignId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

// ---------------------------------------------------------------------------
// Self-serve 10DLC texting registration
// ---------------------------------------------------------------------------

export const TEXTING_REGISTRATION_PATH = "/api/v1/registration/texting";

export type TextingFeeTier = "standard" | "sole_proprietor";

export interface TextingQuote {
  fee_tier: TextingFeeTier;
  /** Not sent to customers any more (totals only); kept optional for older servers. */
  brand_fee_cents?: number;
  campaign_review_cents?: number;
  /** Ringlite's one-time setup fee on top of the carrier fees (absent on older servers). */
  service_fee_cents?: number;
  monthly_cents: number;
  upfront_months: number;
  due_today_cents: number;
}

export type TextingStage =
  | "checkout"
  | "paid"
  | "brand_filed"
  | "otp_pending"
  | "brand_approved"
  | "campaign_filed"
  | "active"
  | "brand_rejected"
  | "campaign_rejected"
  | "needs_attention"
  | "expired"
  // An operator stopped the registration and refunded what the carrier never charged.
  | "cancelled";

export interface TextingRegistration extends TextingQuote {
  id: string;
  stage: TextingStage;
  brand_id: string;
  campaign_id: string;
  brand_status: string | null;
  campaign_status: string | null;
  checkout_url: string | null;
  otp_sent_at: string | null;
  detail: string | null;
}

export interface TextingRegistrationState {
  registration: TextingRegistration | null;
  quotes: { standard: TextingQuote; sole_proprietor: TextingQuote };
  /** Quotes for an already approved business refiling a declined campaign (no brand fee). */
  quotes_resubmit?: { standard: TextingQuote; sole_proprietor: TextingQuote };
}

export interface TextingCheckoutInput {
  brand_id: string;
  campaign_id: string;
  first_name: string;
  last_name: string;
  mobile_phone: string | null;
  sub_usecases: string[];
  assertions: Record<string, boolean>;
}

export const TEXTING_REGISTRATION_KEY = ["registration", "texting"] as const;

/** Stages where the background job is moving things along without the customer. */
export const TEXTING_POLL_STAGES: readonly TextingStage[] = [
  "paid",
  "brand_filed",
  "brand_approved",
  "campaign_filed",
];

export const TEXTING_POLL_MS = 15_000;

/**
 * The path for a single texting registration, with the id percent-encoded. Both the OTP
 * verify and resend endpoints hang off this, and both must not let a stray `/` in the id
 * re-route the request.
 */
function textingRegistrationPath(registrationId: string): string {
  return `${TEXTING_REGISTRATION_PATH}/${encodeURIComponent(registrationId)}`;
}

export interface TextingEvent {
  id: string;
  subject: string;
  telnyx_id: string;
  event_type: string;
  payload_type: string | null;
  status: string | null;
  reasons: string[];
  verified: boolean;
  received_at: string;
}

export async function fetchTextingEvents(
  api: ApiClient,
  registrationId: string,
): Promise<{ items: TextingEvent[] }> {
  return api.request<{ items: TextingEvent[] }>(
    `${textingRegistrationPath(registrationId)}/events`,
  );
}

export async function fetchTextingRegistration(
  api: ApiClient,
): Promise<TextingRegistrationState> {
  return api.request<TextingRegistrationState>(TEXTING_REGISTRATION_PATH);
}

export async function startTextingCheckout(
  api: ApiClient,
  input: TextingCheckoutInput,
): Promise<TextingRegistration> {
  // The input is sent as-is; the component owns the shape (blank names, nulled mobile, the
  // canonical sub-use-case order, the attestation map + terms flag).
  return api.request<TextingRegistration>(`${TEXTING_REGISTRATION_PATH}/checkout`, {
    method: "POST",
    json: input,
  });
}

export async function verifyTextingOtp(
  api: ApiClient,
  registrationId: string,
  pin: string,
): Promise<TextingRegistration> {
  return api.request<TextingRegistration>(`${textingRegistrationPath(registrationId)}/otp`, {
    method: "POST",
    json: { pin },
  });
}

export async function resendTextingOtp(
  api: ApiClient,
  registrationId: string,
): Promise<TextingRegistration> {
  // No body: the endpoint looks up the stored registration and re-sends the code.
  return api.request<TextingRegistration>(
    `${textingRegistrationPath(registrationId)}/otp/resend`,
    { method: "POST" },
  );
}

export function useTextingRegistration(api: ApiClient) {
  return useQuery({
    queryKey: TEXTING_REGISTRATION_KEY,
    queryFn: () => fetchTextingRegistration(api),
    retry: false,
    refetchInterval: (query) => {
      const stage = query.state.data?.registration?.stage;
      if (stage === undefined) return false;
      return TEXTING_POLL_STAGES.includes(stage) ? TEXTING_POLL_MS : false;
    },
  });
}

export function useTextingEvents(api: ApiClient, registrationId: string | null | undefined) {
  return useQuery({
    queryKey: [...TEXTING_REGISTRATION_KEY, "events", registrationId ?? null],
    queryFn: () => fetchTextingEvents(api, registrationId as string),
    enabled: Boolean(registrationId),
    retry: false,
  });
}

export function useTextingCheckout(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: TextingCheckoutInput) => startTextingCheckout(api, input),
    onSuccess: () => {
      // The registration state, the brand list and the campaign list all live under the
      // same `registration` prefix, so one invalidation refreshes every panel.
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useVerifyTextingOtp(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (variables: { registrationId: string; pin: string }) =>
      verifyTextingOtp(api, variables.registrationId, variables.pin),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}

export function useResendTextingOtp(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (registrationId: string) => resendTextingOtp(api, registrationId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}
