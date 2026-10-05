import type { ApiClient } from "./client";
import type { Assistant } from "./assistants";

export interface Interview {
  business: {
    name: string;
    type: string;
    hours: string;
    timezone: string;
    address: string;
    website: string;
    service_area: string;
  };
  agent: { name: string; voice_id: string; language: string; greeting: string };
  goal: string;
  should_do: string[];
  never_do: string[];
  faqs: { q: string; a: string }[];
  handoff: { when: string[]; transfer_number: string };
  booking: { enabled: boolean; rules: string };
  after_call: { summary: boolean; fields: string[] };
}

export type PromptMode = "interview" | "custom";

export interface AgentTemplate {
  id: string;
  version: number;
  name: string;
  channel: "voice" | "sms";
  summary: string;
  available: boolean;
  unavailable_reason: string | null;
  interview: Interview;
}

export interface RenderedPrompt {
  prompt: string;
  greeting: string;
  locked: string[];
  missing: string[];
}

export interface AgentVoice {
  id: string;
  name: string;
  gender: string;
  accent: string;
  description: string;
  provider: "elevenlabs";
}

export const TEMPLATES_KEY = ["agent-templates"] as const;
export const VOICES_KEY = ["agent-voices"] as const;

export function emptyInterview(): Interview {
  return {
    business: { name: "", type: "", hours: "", timezone: "", address: "", website: "", service_area: "" },
    agent: { name: "", voice_id: "", language: "en", greeting: "" },
    goal: "",
    should_do: [],
    never_do: [],
    faqs: [],
    handoff: { when: [], transfer_number: "" },
    booking: { enabled: false, rules: "" },
    after_call: { summary: true, fields: [] },
  };
}

/** Fill any section the server omitted so the form never reads through undefined. */
export function normalizeInterview(raw: Partial<Interview> | null | undefined): Interview {
  const base = emptyInterview();
  const r = raw ?? {};
  return {
    business: { ...base.business, ...(r.business ?? {}) },
    agent: { ...base.agent, ...(r.agent ?? {}) },
    goal: r.goal ?? "",
    should_do: r.should_do ?? [],
    never_do: r.never_do ?? [],
    faqs: r.faqs ?? [],
    handoff: { ...base.handoff, ...(r.handoff ?? {}), when: r.handoff?.when ?? [] },
    booking: { ...base.booking, ...(r.booking ?? {}) },
    after_call: { ...base.after_call, ...(r.after_call ?? {}), fields: r.after_call?.fields ?? [] },
  };
}

export async function listTemplates(api: ApiClient): Promise<AgentTemplate[]> {
  return api.request<AgentTemplate[]>("/api/v1/agent/templates");
}

export async function renderPrompt(
  api: ApiClient,
  body: { template_id: string | null; interview: Interview },
): Promise<RenderedPrompt> {
  return api.request<RenderedPrompt>("/api/v1/agent/templates/render", { method: "POST", json: body });
}

export async function createFromTemplate(
  api: ApiClient,
  body: { template_id: string | null; name: string; interview: Interview },
): Promise<Assistant> {
  return api.request<Assistant>("/api/v1/agent/profiles/from-template", { method: "POST", json: body });
}

export async function listVoices(api: ApiClient): Promise<AgentVoice[]> {
  return api.request<AgentVoice[]>("/api/v1/agent/voices");
}
