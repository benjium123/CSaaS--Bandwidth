import type { ApiClient } from "./client";

/**
 * P26 "inbox pro": private notes with @mentions, snooze, SLA targets and the bell.
 *
 * One file owns every P26 path and shape so the composer, the thread header, the inbox
 * column, the rail bell and the inbox settings page all agree. The shapes below are
 * copied from the staged backend tree (P26 backend VERDICT §1):
 *   - notes      backend/app/api/routes/conversations.py  NoteOut / NoteIn / NoteMention
 *   - snooze     backend/app/api/routes/conversations.py  SnoozeIn
 *   - bell       backend/app/api/routes/me.py             NotificationOut / NotificationsOut
 *   - templates  backend/app/api/routes/templates.py      TemplateOut (+ the new `?q=`)
 * The two SLA minute fields live on the existing Inbox shape in api/conversations.ts,
 * because they are returned by the same GET /api/v1/inboxes the whole app already uses.
 */

// ----------------------------------------------------------------------------------
// Notes
// ----------------------------------------------------------------------------------
export interface NoteMention {
  user_id: string;
  name: string;
}

export interface ThreadNote {
  id: string;
  thread_id: string;
  author_user_id: string | null;
  author_name: string;
  body: string;
  mentions: NoteMention[];
  created_at: string;
}

/** The server parses `@Name` tokens out of the body itself and unions them with whatever
 * ids we send, so `mention_user_ids` is a hint, not the authority. We still send it: the
 * picker knows exactly who was chosen, and a display name that is not a whole-token match
 * (two teammates called "Sam") would otherwise be missed. */
export async function postThreadNote(
  api: ApiClient,
  threadId: string,
  body: string,
  mentionUserIds: string[],
): Promise<ThreadNote> {
  return api.request<ThreadNote>(`/api/v1/conversations/${threadId}/notes`, {
    method: "POST",
    json: { body, mention_user_ids: mentionUserIds },
  });
}

export async function fetchThreadNotes(api: ApiClient, threadId: string): Promise<ThreadNote[]> {
  return api.request<ThreadNote[]>(`/api/v1/conversations/${threadId}/notes`);
}

// ----------------------------------------------------------------------------------
// Snooze
// ----------------------------------------------------------------------------------
export interface SnoozeResult {
  id: string;
  snoozed_until: string | null;
}

export async function snoozeThread(
  api: ApiClient,
  threadId: string,
  until: Date,
): Promise<SnoozeResult> {
  return api.request<SnoozeResult>(`/api/v1/conversations/${threadId}/snooze`, {
    method: "POST",
    json: { until: until.toISOString() },
  });
}

export async function unsnoozeThread(api: ApiClient, threadId: string): Promise<SnoozeResult> {
  return api.request<SnoozeResult>(`/api/v1/conversations/${threadId}/snooze`, {
    method: "DELETE",
  });
}

export type SnoozePresetId = "1h" | "3h" | "tomorrow" | "next-week";

export interface SnoozePreset {
  id: SnoozePresetId;
  label: string;
  /** Pure: given "now", the moment the conversation comes back. Exported so the tests
   * can assert the arithmetic without freezing the clock inside a component. */
  at: (now: Date) => Date;
}

/** Plain words, in the order the menu shows them. "Tomorrow" and "Next week" both land
 * at 9:00 in the viewer's own timezone - `setHours` is local time, which is what a
 * person means by "tomorrow morning". */
export const SNOOZE_PRESETS: SnoozePreset[] = [
  { id: "1h", label: "In 1 hour", at: (now) => new Date(now.getTime() + 60 * 60 * 1000) },
  { id: "3h", label: "In 3 hours", at: (now) => new Date(now.getTime() + 3 * 60 * 60 * 1000) },
  {
    id: "tomorrow",
    label: "Tomorrow at 9am",
    at: (now) => {
      const next = new Date(now);
      next.setDate(next.getDate() + 1);
      next.setHours(9, 0, 0, 0);
      return next;
    },
  },
  {
    id: "next-week",
    label: "Next week",
    at: (now) => {
      const next = new Date(now);
      next.setDate(next.getDate() + 7);
      next.setHours(9, 0, 0, 0);
      return next;
    },
  },
];

/** `<input type="datetime-local">` speaks "YYYY-MM-DDTHH:mm" in LOCAL time and has no
 * timezone at all, so `new Date(value)` (which is what we want here - local) is the only
 * correct reading. Returns null for an unparseable or past value so the caller can keep
 * the button disabled rather than sending a 422. */
export function parseLocalDateTime(value: string, now: Date = new Date()): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  if (parsed.getTime() <= now.getTime()) return null;
  return parsed;
}

// ----------------------------------------------------------------------------------
// SLA
// ----------------------------------------------------------------------------------
export interface Sla {
  due_at: string | null;
  breached: boolean;
}

export type SlaState =
  | { kind: "none" }
  | { kind: "due"; label: string; minutes: number }
  | { kind: "overdue"; label: string };

/** Pure, so both the list row and the thread header render the same words, and the test
 * can drive it with a fixed clock. "Overdue" is the only word for a breach - the caller
 * paints it with text-destructive. */
export function slaState(sla: Sla | null | undefined, now: Date = new Date()): SlaState {
  if (!sla) return { kind: "none" };
  if (sla.breached) return { kind: "overdue", label: "Overdue" };
  if (!sla.due_at) return { kind: "none" };
  const dueMs = new Date(sla.due_at).getTime();
  if (Number.isNaN(dueMs)) return { kind: "none" };
  const remainingMs = dueMs - now.getTime();
  if (remainingMs <= 0) return { kind: "overdue", label: "Overdue" };
  const minutes = Math.max(1, Math.round(remainingMs / 60000));
  if (minutes < 60) return { kind: "due", label: `${minutes}m left`, minutes };
  const hours = Math.round(minutes / 60);
  if (hours < 48) return { kind: "due", label: `${hours}h left`, minutes };
  return { kind: "due", label: `${Math.round(hours / 24)}d left`, minutes };
}

// ----------------------------------------------------------------------------------
// Templates (the composer's "/" quick-pick)
// ----------------------------------------------------------------------------------
export interface MessageTemplate {
  id: string;
  name: string;
  body: string;
  media_asset_ids: string[];
  /** Merge fields the body uses, e.g. ["contact.first_name"] - the backend extracts
   * them; we show them so a person knows the reply is not finished text. */
  tokens: string[];
}

export async function fetchTemplates(api: ApiClient, q?: string): Promise<MessageTemplate[]> {
  const search = q && q.trim() ? `?q=${encodeURIComponent(q.trim())}` : "";
  return api.request<MessageTemplate[]>(`/api/v1/templates${search}`);
}

// ----------------------------------------------------------------------------------
// Bell
// ----------------------------------------------------------------------------------
export type NotificationKind =
  | "mention"
  | "assignment"
  | "overdue"
  | "missed_call"
  | "low_balance";

export interface Notification {
  id: string;
  kind: string;
  thread_id: string | null;
  // Set whenever thread_id is, so the bell can navigate straight to the conversation -
  // it is addressed by this number pair, not by thread id (P20c).
  our_e164: string | null;
  contact_e164: string | null;
  body: string;
  read_at: string | null;
  created_at: string;
}

export interface NotificationsPage {
  items: Notification[];
  unread_count: number;
}

/** Plain words for each kind, used as the group heading in the bell menu. An unknown
 * kind falls back to "Other" rather than showing the raw wire value. */
export const NOTIFICATION_KIND_LABELS: Record<string, string> = {
  mention: "Mentions",
  assignment: "Assigned to you",
  overdue: "Overdue",
  missed_call: "Missed calls",
  low_balance: "Balance",
};

export function notificationKindLabel(kind: string): string {
  return NOTIFICATION_KIND_LABELS[kind] ?? "Other";
}

export async function fetchNotifications(
  api: ApiClient,
  options: { unread?: boolean; limit?: number } = {},
): Promise<NotificationsPage> {
  const params = new URLSearchParams();
  if (options.unread) params.set("unread", "1");
  if (options.limit) params.set("limit", String(options.limit));
  const qs = params.toString();
  return api.request<NotificationsPage>(`/api/v1/me/notifications${qs ? `?${qs}` : ""}`);
}

export async function markNotificationsRead(
  api: ApiClient,
  target: { ids: string[] } | { all: true },
): Promise<{ updated: number }> {
  return api.request<{ updated: number }>("/api/v1/me/notifications/read", {
    method: "POST",
    json: target,
  });
}

/** The realtime event the bell listens for (backend services/notifications.py publishes
 * it on the same socket the softphone already owns). `user_id` is present so the WS gate
 * can address it; the client still filters on it, because a future fan-out change must
 * not turn into someone else's bell appearing in this one. */
export interface NotificationCreatedEvent {
  type: "notification.created";
  user_id: string | null;
  notification_id: string;
  kind: string;
  thread_id: string | null;
  our_e164: string | null;
  contact_e164: string | null;
  body: string;
  created_at: string | null;
}

export function isNotificationCreated(
  event: Record<string, unknown> & { type?: string },
): event is NotificationCreatedEvent & Record<string, unknown> {
  return event.type === "notification.created";
}
