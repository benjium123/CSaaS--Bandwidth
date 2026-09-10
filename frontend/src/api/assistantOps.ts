/**
 * P23b — the one place that knows the assistant *operations* wire (P23B_HANDOFF.md).
 *
 * Kept out of `api/assistants.ts` on purpose: that file is the P23a builder's contract and
 * is imported by five surfaces, whereas everything here is new in P23b and is read by the
 * flows editor, the numbers table, the campaigns form, the builder's new controls, the
 * inbox timeline and the dashboard. One file, one place to change if the backend moves a
 * path.
 *
 * Nothing here invents a shape the handoff does not name. Where a response shape is the
 * frontend's guess (the number's CURRENT "answered by", which the handoff specifies only as
 * a PATCH), it is read defensively and the VERDICT lists it as an open item.
 */
import { postAuthedBlob, type ApiClient } from "./client";

/* --------------------------------------------------------------------------------------
 * Voice preview — POST /api/v1/agent/voices/preview -> audio/mpeg (<= 3 s)
 * ------------------------------------------------------------------------------------ */

export const VOICE_PREVIEW_PATH = "/api/v1/agent/voices/preview";

/** The sentence we ask the assistant's voice to say. Short on purpose: the backend caches
 * per (provider, voice, text hash), so a stable string means the second press of Preview is
 * free for everyone in the org. */
export const VOICE_PREVIEW_TEXT =
  "Hi, thanks for calling. How can I help you today?";

export async function previewVoice(
  api: ApiClient,
  input: { tts_provider: string; voice_id: string; text?: string },
): Promise<Blob> {
  return postAuthedBlob(api, VOICE_PREVIEW_PATH, {
    tts_provider: input.tts_provider,
    voice_id: input.voice_id,
    text: input.text ?? VOICE_PREVIEW_TEXT,
  });
}

/* --------------------------------------------------------------------------------------
 * "Call me" — POST /api/v1/agent/profiles/{id}/call-me {to_e164}
 * ------------------------------------------------------------------------------------ */

export interface CallMeOut {
  call_id?: string;
  status?: string;
}

export async function callMe(
  api: ApiClient,
  profileId: string,
  toE164: string,
): Promise<CallMeOut> {
  return api.request<CallMeOut>(`/api/v1/agent/profiles/${profileId}/call-me`, {
    method: "POST",
    json: { to_e164: toE164 },
  });
}

/** A deliberately forgiving check: the field is a phone number the admin types by hand, and
 * the backend is the authority on whether it can be dialled. This only stops the obviously
 * empty / obviously-not-a-number submit, so the button's disabled state is honest. */
export function looksLikePhoneNumber(value: string): boolean {
  const digits = value.replace(/\D/g, "");
  return digits.length >= 10 && digits.length <= 15;
}

/** Normalises what the admin typed into what the wire wants. A 10-digit entry is assumed
 * to be North American, which is the only assumption the rest of this app already makes
 * (see lib/format's formatPhone). Anything already carrying a "+" is passed through. */
export function toE164(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith("+")) return `+${trimmed.slice(1).replace(/\D/g, "")}`;
  const digits = trimmed.replace(/\D/g, "");
  if (digits.length === 10) return `+1${digits}`;
  return `+${digits}`;
}

/* --------------------------------------------------------------------------------------
 * Analytics — GET /api/v1/analytics/assistant?from&to
 * ------------------------------------------------------------------------------------ */

export interface AssistantAnalytics {
  calls: number;
  minutes: number;
  answer_rate: number;
  handoff_rate: number;
  booked: number;
  avg_duration_seconds: number;
  /** Null whenever the org cannot see cost (platform keys, or no cost recorded yet). The
   * tile then omits the cost figure entirely rather than showing a confident "$0.00". */
  cost_micros?: number | null;
}

export const ASSISTANT_ANALYTICS_KEY = ["assistant-analytics"] as const;

export async function fetchAssistantAnalytics(
  api: ApiClient,
  range: { from: string; to: string },
): Promise<AssistantAnalytics> {
  const params = new URLSearchParams({ from: range.from, to: range.to });
  return api.request<AssistantAnalytics>(`/api/v1/analytics/assistant?${params.toString()}`);
}

/** `days` -> the inclusive [from, to] pair the endpoint wants, as plain YYYY-MM-DD. Pure so
 * the range maths is testable without a clock mock; `now` is injectable for the same reason. */
export function analyticsRange(days: number, now: Date = new Date()): { from: string; to: string } {
  const to = new Date(now.getTime());
  const from = new Date(now.getTime() - (days - 1) * 24 * 60 * 60 * 1000);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  return { from: iso(from), to: iso(to) };
}

export const ANALYTICS_RANGE_DAYS = [7, 30, 90] as const;
export type AnalyticsRangeDays = (typeof ANALYTICS_RANGE_DAYS)[number];

/** Percentages arrive as a 0..1 ratio. A missing/NaN ratio renders "—", never "0%": a rate
 * we do not have is not a rate of zero. */
export function formatRate(ratio: number | null | undefined): string {
  if (ratio == null || Number.isNaN(ratio)) return "—";
  return `${Math.round(ratio * 1000) / 10}%`;
}

export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes)) return "—";
  return `${Math.round(minutes)}`;
}

export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const whole = Math.round(seconds);
  const mins = Math.floor(whole / 60);
  const secs = whole % 60;
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

export function formatCostMicros(micros: number | null | undefined): string | null {
  if (micros == null || Number.isNaN(micros)) return null;
  return `$${(micros / 1_000_000).toFixed(2)}`;
}

/* --------------------------------------------------------------------------------------
 * The AI call card's payload — CallTimelineEvent.assistant (P23B_HANDOFF.md)
 * ------------------------------------------------------------------------------------ */

/** Declared here rather than in `api/conversations.ts` so that file takes a TWO-LINE diff
 * (one import, one field). P26 is rewriting `api/conversations.ts` in parallel and the
 * integrator has to merge this by hand — see VERDICT.md. */
export interface AssistantCallSummary {
  summary: string | null;
  disposition: string | null;
  sentiment: string | null;
  has_transcript: boolean;
  /** Not in the handoff's list, but a name is what the card wants to show and the backend
   * may well send it. Absent = the card says "Assistant" without naming which one. */
  name?: string | null;
}

/** Backend dispositions are snake_case enum values. Anything this map does not know is
 * turned into plain words rather than shown raw — a customer must never read "no_answer". */
const DISPOSITION_WORDS: Record<string, string> = {
  booked: "Booked",
  qualified: "Qualified",
  not_qualified: "Not qualified",
  handoff: "Passed to a person",
  handed_off: "Passed to a person",
  transferred: "Passed to a person",
  voicemail: "Left a voicemail",
  no_answer: "No answer",
  not_interested: "Not interested",
  callback: "Call back later",
  wrong_number: "Wrong number",
  do_not_call: "Asked not to be contacted",
  completed: "Completed",
  failed: "Did not finish",
};

export function dispositionWords(value: string | null | undefined): string | null {
  if (!value) return null;
  const known = DISPOSITION_WORDS[value];
  if (known) return known;
  const spaced = value.replace(/[_-]+/g, " ").trim();
  if (!spaced) return null;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export type PillTone = "neutral" | "success" | "warning" | "danger" | "info";

export function dispositionTone(value: string | null | undefined): PillTone {
  switch (value) {
    case "booked":
    case "qualified":
    case "completed":
      return "success";
    case "handoff":
    case "handed_off":
    case "transferred":
    case "callback":
      return "info";
    case "no_answer":
    case "voicemail":
      return "warning";
    case "do_not_call":
    case "not_interested":
    case "wrong_number":
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

const SENTIMENT_WORDS: Record<string, string> = {
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
  mixed: "Mixed",
};

export function sentimentWords(value: string | null | undefined): string | null {
  if (!value) return null;
  return SENTIMENT_WORDS[value] ?? dispositionWords(value);
}
