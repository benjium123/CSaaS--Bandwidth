import type { ApiClient } from "./client";

export type ProviderName = "bandwidth" | "telnyx" | "twilio" | "plivo" | "signalwire";
export type ProviderAccountStatus = "unverified" | "active" | "failed" | "disabled";

/** What the backend echoes back for a stored secret field - never the plaintext. */
export const SECRET_MASK = "•••••";

export interface ProviderAccount {
  id: string;
  provider: ProviderName;
  label: string;
  status: ProviderAccountStatus;
  last_probe_at: string | null;
  last_probe_detail: string | null;
  credentials: Record<string, string>;
}

export interface ProviderField {
  name: string;
  label: string;
  secret: boolean;
}

export const PROVIDER_FIELDS: Record<ProviderName, ProviderField[]> = {
  bandwidth: [
    { name: "account_id", label: "Account ID", secret: false },
    { name: "api_username", label: "API username", secret: false },
    { name: "api_password", label: "API password", secret: true },
    { name: "messaging_application_id", label: "Messaging application ID", secret: false },
    { name: "voice_application_id", label: "Voice application ID", secret: false },
    { name: "webhook_username", label: "Webhook username", secret: false },
    { name: "webhook_password", label: "Webhook password", secret: true },
    { name: "site_id", label: "Site ID (needed to order numbers)", secret: false },
  ],
  telnyx: [
    { name: "api_key", label: "API key", secret: true },
    { name: "public_key", label: "Public key", secret: true },
    { name: "messaging_profile_id", label: "Messaging profile ID", secret: false },
    { name: "voice_connection_id", label: "Voice connection ID", secret: false },
  ],
  twilio: [
    { name: "account_sid", label: "Account SID", secret: false },
    { name: "auth_token", label: "Auth token", secret: true },
    { name: "messaging_service_sid", label: "Messaging service SID", secret: false },
  ],
  plivo: [
    { name: "auth_id", label: "Auth ID", secret: false },
    { name: "auth_token", label: "Auth token", secret: true },
    { name: "powerpack_uuid", label: "Powerpack UUID", secret: false },
  ],
  signalwire: [
    { name: "project_id", label: "Project ID", secret: false },
    { name: "api_token", label: "API token", secret: true },
    { name: "space_url", label: "Space URL", secret: false },
  ],
};

export const PROVIDER_NAMES: ProviderName[] = [
  "bandwidth",
  "telnyx",
  "twilio",
  "plivo",
  "signalwire",
];

export interface CreateProviderAccountInput {
  provider: ProviderName;
  label: string;
  credentials: Record<string, string>;
}

export interface PatchProviderAccountInput {
  label?: string;
  credentials?: Record<string, string>;
}

export async function fetchProviderAccounts(api: ApiClient): Promise<ProviderAccount[]> {
  return api.request<ProviderAccount[]>("/api/v1/provider-accounts");
}

export async function createProviderAccount(
  api: ApiClient,
  input: CreateProviderAccountInput,
): Promise<ProviderAccount> {
  return api.request<ProviderAccount>("/api/v1/provider-accounts", {
    method: "POST",
    json: input,
  });
}

export async function patchProviderAccount(
  api: ApiClient,
  id: string,
  input: PatchProviderAccountInput,
): Promise<ProviderAccount> {
  return api.request<ProviderAccount>(`/api/v1/provider-accounts/${id}`, {
    method: "PATCH",
    json: input,
  });
}

export async function probeProviderAccount(
  api: ApiClient,
  id: string,
): Promise<ProviderAccount> {
  return api.request<ProviderAccount>(`/api/v1/provider-accounts/${id}/probe`, {
    method: "POST",
  });
}

export async function disableProviderAccount(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/provider-accounts/${id}`, { method: "DELETE" });
}
