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
}

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

export function useSubmitCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (campaignId: string) => submitCampaign(api, campaignId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["registration"], exact: false });
    },
  });
}
