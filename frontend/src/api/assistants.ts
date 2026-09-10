import type { ApiClient, ApiError } from "./client";

export type AiKind = "llm" | "stt" | "tts";

export const AI_KINDS: AiKind[] = ["llm", "stt", "tts"];

export const AI_KIND_WORDS: Record<AiKind, string> = {
  llm: "language model",
  stt: "speech recognition",
  tts: "voice",
};

export const AI_KIND_HELP: Record<AiKind, string> = {
  llm: "Writes what your assistant says.",
  stt: "Turns what the caller says into text.",
  tts: "Speaks your assistant's words out loud.",
};

export const AI_PROVIDERS_BY_KIND: Record<AiKind, string[]> = {
  llm: ["openai", "anthropic", "deepseek", "groq", "google"],
  stt: ["deepgram", "assemblyai"],
  tts: ["elevenlabs", "cartesia"],
};

export const AI_PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  groq: "Groq",
  google: "Google",
  deepgram: "Deepgram",
  assemblyai: "AssemblyAI",
  elevenlabs: "ElevenLabs",
  cartesia: "Cartesia",
};

export function aiProviderLabel(name: string): string {
  return AI_PROVIDER_LABELS[name] ?? name;
}

export interface AiProviderField {
  name: string;
  label: string;
  secret: boolean;
}

export const AI_PROVIDER_FIELDS: Record<string, AiProviderField[]> = {
  openai: [{ name: "api_key", label: "API key", secret: true }],
  anthropic: [{ name: "api_key", label: "API key", secret: true }],
  deepseek: [{ name: "api_key", label: "API key", secret: true }],
  groq: [{ name: "api_key", label: "API key", secret: true }],
  google: [{ name: "api_key", label: "API key", secret: true }],
  deepgram: [{ name: "api_key", label: "API key", secret: true }],
  assemblyai: [{ name: "api_key", label: "API key", secret: true }],
  elevenlabs: [
    { name: "api_key", label: "API key", secret: true },
    { name: "default_voice_id", label: "Default voice id", secret: false },
  ],
  cartesia: [{ name: "api_key", label: "API key", secret: true }],
};

function humanFieldLabel(name: string): string {
  const tokens = name.split("_");
  return tokens
    .map((token, index) => {
      if (index === 0) {
        if (token.toLowerCase() === "api") return "API";
        return token.charAt(0).toUpperCase() + token.slice(1);
      }
      return token;
    })
    .join(" ");
}

export function fieldsForAccount(account: AiProviderAccount): AiProviderField[] {
  // The create form has no server row yet, so it needs the local catalogue; an existing
  // account trusts the server's field map because it may know fields this client build
  // does not.
  const serverFields = account.fields;
  if (serverFields && Object.keys(serverFields).length > 0) {
    return Object.keys(serverFields).map((name) => ({
      name,
      label: humanFieldLabel(name),
      secret: Boolean(serverFields[name]),
    }));
  }
  return AI_PROVIDER_FIELDS[account.provider] ?? [];
}

export type AiProviderStatus = "unverified" | "active" | "failed" | "disabled";

export interface AiProviderAccount {
  id: string;
  kind: AiKind;
  provider: string;
  label: string;
  status: AiProviderStatus;
  last_probe_at: string | null;
  last_probe_detail: string | null;
  fields?: Record<string, boolean>;
  has_credentials?: boolean;
}

export interface AiSettings {
  ai_key_mode: "platform" | "byok";
  byok_ready: boolean;
  missing_kinds: AiKind[];
}

export function missingKindWords(kinds: AiKind[]): string {
  if (kinds.length === 0) return "";
  if (kinds.length === 1) return AI_KIND_WORDS[kinds[0]];
  if (kinds.length === 2) return `${AI_KIND_WORDS[kinds[0]]} and ${AI_KIND_WORDS[kinds[1]]}`;
  const words = kinds.map((kind) => AI_KIND_WORDS[kind]);
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

export async function fetchAiProviders(api: ApiClient): Promise<AiProviderAccount[]> {
  return api.request<AiProviderAccount[]>("/api/v1/ai/providers");
}

export async function createAiProvider(
  api: ApiClient,
  input: { kind: AiKind; provider: string; label?: string; credentials: Record<string, string> },
): Promise<AiProviderAccount> {
  return api.request<AiProviderAccount>("/api/v1/ai/providers", {
    method: "POST",
    json: input,
  });
}

export interface PatchAiProviderInput {
  label?: string;
  credentials?: Record<string, string>;
  status?: "disabled" | "unverified";
}

export async function patchAiProvider(
  api: ApiClient,
  id: string,
  body: PatchAiProviderInput,
): Promise<AiProviderAccount> {
  return api.request<AiProviderAccount>(`/api/v1/ai/providers/${id}`, {
    method: "PATCH",
    json: body,
  });
}

export async function deleteAiProvider(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/ai/providers/${id}`, { method: "DELETE" });
}

export async function probeAiProvider(
  api: ApiClient,
  id: string,
): Promise<{ status: AiProviderStatus; detail: string | null }> {
  return api.request<{ status: AiProviderStatus; detail: string | null }>(
    `/api/v1/ai/providers/${id}/probe`,
    { method: "POST" },
  );
}

export async function fetchAiSettings(api: ApiClient): Promise<AiSettings> {
  return api.request<AiSettings>("/api/v1/ai/settings");
}

export async function patchAiSettings(
  api: ApiClient,
  body: { ai_key_mode: "platform" | "byok" },
): Promise<AiSettings> {
  return api.request<AiSettings>("/api/v1/ai/settings", {
    method: "PATCH",
    json: body,
  });
}

export const AI_PROVIDERS_KEY = ["ai-providers"] as const;
export const AI_SETTINGS_KEY = ["ai-settings"] as const;
// The same key the existing `useAgentProfiles` in api/hooks.ts uses, so both stay in sync.
export const ASSISTANTS_KEY = ["agent-profiles"] as const;
export const KB_DOCUMENTS_KEY = ["assistant-kb-documents"] as const;

export type AssistantToolName =
  | "book_appointment"
  | "transfer"
  | "send_followup_sms"
  | "lookup_contact"
  | "webhook";

// Flat on the wire: {tool, ...config} — no nested `config`, no `type`. Only enabled
// tools are ever sent; a disabled tool is simply absent from the list.
export interface AssistantTool {
  tool: AssistantToolName;
  [key: string]: string | undefined;
}

export interface PostCallField {
  name: string;
  type: "text" | "number" | "date" | "select";
  options?: string[];
  write_to_attribute?: string;
}

export interface Assistant {
  id: string;
  name: string;
  system_prompt: string;
  greeting: string;
  voice_id: string;
  llm_provider: string;
  llm_model: string;
  voicemail_message: string;
  is_default: boolean;
  goals?: string;
  guardrails?: string;
  language?: string;
  stt_provider?: string;
  tts_provider?: string;
  max_call_seconds?: number;
  silence_timeout_seconds?: number;
  interrupt_sensitivity?: string;
  voicemail_action?: string;
  tools?: AssistantTool[];
  post_call_fields?: PostCallField[];
  effective_prompt?: string | null;
  sms_enabled?: boolean;
  sms_turn_ceiling?: number;
  sms_handoff_keywords?: string[];
  sms_max_reply_chars?: number;
}

export async function fetchAssistants(api: ApiClient): Promise<Assistant[]> {
  return api.request<Assistant[]>("/api/v1/agent/profiles");
}

export async function createAssistant(
  api: ApiClient,
  body: Partial<Assistant>,
): Promise<Assistant> {
  return api.request<Assistant>("/api/v1/agent/profiles", {
    method: "POST",
    json: body,
  });
}

export async function patchAssistant(
  api: ApiClient,
  id: string,
  body: Partial<Assistant>,
): Promise<Assistant> {
  return api.request<Assistant>(`/api/v1/agent/profiles/${id}`, {
    method: "PATCH",
    json: body,
  });
}

export async function deleteAssistant(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/agent/profiles/${id}`, { method: "DELETE" });
}

export async function setDefaultAssistant(api: ApiClient, id: string): Promise<Assistant> {
  return api.request<Assistant>(`/api/v1/agent/profiles/${id}/default`, {
    method: "POST",
  });
}

export async function goLiveAssistant(
  api: ApiClient,
  id: string,
): Promise<{ ok?: boolean }> {
  return api.request<{ ok?: boolean }>(`/api/v1/agent/profiles/${id}/go-live`, {
    method: "POST",
  });
}

export interface SimulateTurn {
  role: "user" | "assistant";
  content: string;
}

export interface SimulateOut {
  reply: string;
  tokens_in: number;
  tokens_out: number;
  kb_hits: { document_id: string; title: string; snippet?: string }[];
}

export async function simulateAssistant(
  api: ApiClient,
  id: string,
  messages: SimulateTurn[],
): Promise<SimulateOut> {
  return api.request<SimulateOut>(`/api/v1/agent/profiles/${id}/simulate`, {
    method: "POST",
    json: { messages },
  });
}

export function goLiveBlockers(error: unknown): string[] {
  // `error` really can be null/undefined here (a rejected promise carries whatever it was
  // rejected with), so read `details` off it only once it is an object.
  const details =
    error && typeof error === "object" ? (error as ApiError).details : undefined;
  if (details && typeof details === "object") {
    const record = details as Record<string, unknown>;

    const missingKinds = record.missing_kinds;
    if (Array.isArray(missingKinds)) {
      const sentences = missingKinds
        .filter((kind): kind is AiKind => typeof kind === "string" && kind in AI_KIND_WORDS)
        .map((kind) => `Add a ${AI_KIND_WORDS[kind]} connection.`);
      if (sentences.length > 0) return sentences;
    }

    const missing = Array.isArray(record.missing)
      ? record.missing
      : Array.isArray(record.missing_keys)
        ? record.missing_keys
        : Array.isArray(record.missing_platform_keys)
          ? record.missing_platform_keys
        : null;
    if (missing) {
      const sentences = missing.filter((item): item is string => typeof item === "string");
      if (sentences.length > 0) return sentences;
    }

    const detailMessage = record.detail ?? record.message;
    if (typeof detailMessage === "string") return [detailMessage];
  }

  // The exact 422 body is owned by the backend supervisor, so this reads every shape it
  // might send rather than betting on one; never throw on a malformed body.
  if (error instanceof Error) return [error.message];
  return [];
}

export const KB_DOCUMENTS_PATH = "/api/v1/agent/kb/documents";

export type KbStatus = "pending" | "indexed" | "failed";

export interface KbDocument {
  id: string;
  title: string;
  source: string;
  status: KbStatus;
  chunk_count: number;
  detail?: string | null;
}

export async function fetchKbDocuments(api: ApiClient): Promise<KbDocument[]> {
  return api.request<KbDocument[]>(KB_DOCUMENTS_PATH);
}

export async function deleteKbDocument(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`${KB_DOCUMENTS_PATH}/${id}`, { method: "DELETE" });
}

export async function createKbDocumentFromText(
  api: ApiClient,
  input: { title: string; text: string },
): Promise<KbDocument> {
  return api.request<KbDocument>(KB_DOCUMENTS_PATH, {
    method: "POST",
    json: { title: input.title, text: input.text },
  });
}

export async function createKbDocumentFromUrl(
  api: ApiClient,
  input: { title: string; url: string },
): Promise<KbDocument> {
  return api.request<KbDocument>(KB_DOCUMENTS_PATH, {
    method: "POST",
    json: { title: input.title, url: input.url },
  });
}

export async function uploadKbDocument(
  api: ApiClient,
  input: { title: string; file: File },
): Promise<KbDocument> {
  const form = new FormData();
  form.append("title", input.title);
  form.append("file", input.file);
  // Pass the FormData as `body`, not `json`, so the browser sets the multipart boundary
  // itself.
  return api.request<KbDocument>(KB_DOCUMENTS_PATH, { method: "POST", body: form });
}

export const COMPLIANCE_PREAMBLE =
  "Always included: say you are an automated assistant whenever you are asked. Honour any request to stop contact or to be removed from the list. Never give medical, legal or financial advice.";

export function composeEffectivePrompt(a: {
  system_prompt?: string;
  goals?: string;
  guardrails?: string;
}): string {
  const blocks = [COMPLIANCE_PREAMBLE];
  const fields = [a.system_prompt, a.goals, a.guardrails];
  for (const field of fields) {
    if (field && field.trim() !== "") blocks.push(field);
  }
  return blocks.join("\n\n");
}
