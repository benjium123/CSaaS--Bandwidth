import type { ApiClient } from "@/api/client";

export const MAX_MEDIA_BYTES = 3_750_000;

export const ALLOWED_MEDIA_TYPES: readonly string[] = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "video/mp4",
  "video/3gpp",
  "audio/mpeg",
  "text/vcard",
  "application/pdf",
];

export function attachmentLimitSentence(bytes = MAX_MEDIA_BYTES): string {
  // The server floors to one decimal. Keep the same floor (not round) so a client-side
  // rejection can never offer a different size than the API's 422 sentence.
  const mb = Math.floor((bytes / 1_000_000) * 10) / 10;
  return `Attachments up to ${mb.toFixed(1).replace(/\.0$/, "")} MB`;
}

export interface MediaAttachment {
  id: string;
  content_type: string | null;
  size_bytes: number | null;
  status: string;
  url: string | null;
}

export function mediaRejectionReason(file: {
  type: string;
  size: number;
  name: string;
}): string | null {
  if (file.size > MAX_MEDIA_BYTES) return attachmentLimitSentence();
  if (!ALLOWED_MEDIA_TYPES.includes(file.type)) {
    return "That kind of file can't be sent in a message.";
  }
  return null;
}

export async function uploadMedia(api: ApiClient, file: File): Promise<MediaAttachment> {
  const form = new FormData();
  // Do not pass `json`; FormData must go through `body` so the browser supplies the
  // multipart boundary.
  form.append("file", file);
  return api.request<MediaAttachment>("/api/v1/media", { method: "POST", body: form });
}

/** The server's detector, ported: backend/app/services/links.py URL_RE. Only an absolute
 * http(s) URL is ever shortened, so a bare "example.com" must not count here either -
 * claiming a link will be tracked when the server would leave it alone is the one way
 * this indicator could lie. */
export const TRACKABLE_URL_RE = /https?:\/\/[^\s<>'"]+/g;

const TRAILING_URL_PUNCTUATION = new Set([".", ",", "!", "?", ";", ":", ")", "]", "}", '"', "'"]);

export function trackableUrls(body: string): string[] {
  // `String.match` with a /g regex starts and ends at lastIndex 0, so sharing the module
  // constant across calls is safe. Never use `.test()` on it for the same reason.
  const matches = body.match(TRACKABLE_URL_RE);
  if (!matches) return [];

  const unique = new Set<string>();
  for (const match of matches) {
    let trimmed = match;
    // The server's negative lookbehind backtracks, so it drops EVERY trailing punctuation
    // character, not just the last one ("http://x.com/a)." -> "http://x.com/a"). Trimming
    // only one here would report a different URL than the one actually shortened.
    while (trimmed.length > 0 && TRAILING_URL_PUNCTUATION.has(trimmed[trimmed.length - 1])) {
      trimmed = trimmed.slice(0, -1);
    }
    if (trimmed.length > 0) unique.add(trimmed);
  }
  return Array.from(unique);
}

export interface TrackedLink {
  code: string;
  target_url: string;
  clicks: number;
}

export interface ScheduledMessage {
  id: string;
  thread_id: string;
  direction: string;
  status: string;
  from_e164: string;
  to_e164: string;
  body: string | null;
  error_code: string | null;
  hold_until: string | null;
  created_at: string;
  route_reason: string | null;
  failure_reason_public: string | null;
  scheduled_for: string | null;
  clicks: number;
  links: TrackedLink[];
}

export async function fetchScheduledMessages(
  api: ApiClient,
  threadId?: string,
): Promise<ScheduledMessage[]> {
  const params = new URLSearchParams({ status: "scheduled" });
  if (threadId) params.set("thread_id", threadId);
  return api.request<ScheduledMessage[]>(`/api/v1/messages?${params.toString()}`);
}

export async function cancelScheduledMessage(api: ApiClient, messageId: string): Promise<void> {
  await api.request<void>(`/api/v1/messages/${encodeURIComponent(messageId)}/schedule`, {
    method: "DELETE",
  });
}

export function toIsoWithOffset(localValue: string): string {
  if (!localValue) return "";
  const date = new Date(localValue);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString();
}

export function datetimeLocalMin(now = new Date()): string {
  // `toISOString()` would give the UTC wall time, which is wrong for every user not on
  // UTC. Build the local wall time from local date parts.
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  const hour = String(now.getHours()).padStart(2, "0");
  const minute = String(now.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

export function scheduleRejectionReason(localValue: string, now = new Date()): string | null {
  if (!localValue) return null;

  const date = new Date(localValue);
  if (Number.isNaN(date.getTime())) {
    return "Pick a time in the future to send this later";
  }

  const instant = date.getTime();
  if (instant <= now.getTime()) {
    return "Pick a time in the future to send this later";
  }
  if (instant > now.getTime() + 90 * 24 * 60 * 60 * 1000) {
    return "Messages can be scheduled up to 90 days ahead";
  }
  return null;
}
