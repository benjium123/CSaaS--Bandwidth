import type { ApiClient } from "./client";
import type { NoteMention, Sla } from "./inboxPro";
// P23b: declared in api/assistantOps.ts so THIS file takes a two-line diff - another phase
// is rewriting it in parallel and the integrator has to merge by hand.
import type { AssistantCallSummary } from "./assistantOps";

export type InboxRole = "admin" | "member" | "viewer";

export interface Inbox {
  id: string;
  name: string;
  color: string;
  e164: string;
  number_id: string;
  my_role: InboxRole;
  /** P26: reply/resolve targets in minutes, null when this inbox has none. Optional
   * because older fixtures and callers omit them; the backend InboxOut always sends
   * both (null or a number). */
  sla_first_response_minutes?: number | null;
  sla_resolution_minutes?: number | null;
}

export interface ContactPhone {
  e164: string;
  label: string;
  is_primary: boolean;
}

/** Company/role/email/address live in the backend's free-form `attributes` JSON
 * (backend/app/api/routes/contacts.py ContactOut.attributes) - there is no dedicated
 * column for any of them, so they are read from and written to `attributes` by key. */
export interface ContactAttributes {
  company?: string | null;
  role?: string | null;
  email?: string | null;
  address?: string | null;
  [key: string]: unknown;
}

export interface Contact {
  id: string;
  display_name: string;
  attributes: ContactAttributes;
  notes?: string | null;
  phones: ContactPhone[];
  // P22 ownership fields returned by the backend's ContactOut. Optional because older
  // fixtures/callers omit them, while the backend ContactOut now carries them.
  owner_user_id?: string | null;
  department_id?: string | null;
}

export interface ContactSummary {
  id: string;
  display_name: string | null;
}

export type ConversationEventType = "message" | "call" | "voicemail";

export interface Conversation {
  our_e164: string;
  contact_e164: string;
  inbox_id: string;
  // Item 11/12: a call-only conversation (no message ever sent/received on this pair)
  // has no MessageThread row yet - backend/app/api/routes/conversations.py ConversationOut
  // types this `uuid.UUID | None`. Close/Reopen (thread-status actions) have nothing to
  // act on until a thread exists.
  thread_id: string | null;
  contact: ContactSummary | null;
  snippet: string | null;
  last_event_type: ConversationEventType;
  // Item 36: null when the pair's latest event has no clear direction (backend types
  // this `str | None`) - render neutrally rather than defaulting to either arrow.
  direction: "inbound" | "outbound" | null;
  last_event_at: string;
  // Item 35: the backend sends a plain boolean, not a count.
  unread: boolean;
  status: "open" | "closed";
  /** Starred/important pair (POST /api/v1/inbox/important-pair). Optional because older
   * callers/fixtures may omit it entirely - treat undefined the same as false. */
  important?: boolean;
  /** P26: set while this conversation is put off until a time. The `open` list excludes
   * a live snooze, so a row carrying this only shows up under the Snoozed view. */
  snoozed_until?: string | null;
  /** P26: reply-by time for this row, or a breach. Null when the inbox has no target. */
  sla?: Sla | null;
}

export type ConversationTab = "chats" | "calls";
export type ConversationFilter =
  | "open"
  | "unread"
  | "unresponded"
  | "important"
  | "all"
  | "snoozed"
  | "overdue";

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface MessageTimelineItem {
  kind: "message";
  id: string;
  direction: "inbound" | "outbound";
  body: string | null;
  media: { url: string; content_type?: string | null }[] | null;
  status: string;
  occurred_at: string;
  error_code: string | null;
  // P21: matches backend/app/api/routes/messages.py MessageOut.route_reason - a plain
  // sentence explaining which provider carried this message and why, e.g. "Sent via
  // Telnyx - cheapest healthy route". Null when smart routing has nothing to say.
  route_reason: string | null;
}

/** Matches backend/app/api/routes/conversations.py CallRecordingOut - the draft typed
 * this as a bare string, but the API returns an object (no download URL; the client
 * builds one from `id` + the call's own `id` via GET /calls/{call_id}/recordings/{id},
 * the same endpoint CallsPage.tsx already uses). */
export interface CallTimelineRecording {
  id: string;
  status: string;
  duration_seconds: number | null;
}

export interface CallTimelineItem {
  kind: "call";
  id: string;
  direction: "inbound" | "outbound";
  status: string;
  duration_seconds: number | null;
  occurred_at: string;
  answered_at: string | null;
  ended_at: string | null;
  failure_detail: string | null;
  recording: CallTimelineRecording | null;
  has_voicemail: boolean;
  // P21: matches backend/app/api/routes/calls.py CallOut.route_reason (same sentence as
  // messages - e.g. "Failed over to Telnyx - Bandwidth unavailable", or "Via your calling
  // trunk" for LiveKit human calls). Null when smart routing has nothing to say.
  route_reason: string | null;
  /** P23b: present and non-null only when an assistant took this call. */
  assistant?: AssistantCallSummary | null;
}

export interface VoicemailTimelineItem {
  kind: "voicemail";
  id: string;
  call_id: string;
  occurred_at: string;
  transcript: string | null;
  duration_seconds: number | null;
  transcript_status: string;
  /** Backend follow-up: voicemails now carry their own recording, the same shape as
   * CallTimelineItem.recording - played the same way, via call_id + recording.id. */
  recording: CallTimelineRecording | null;
}

/** P26: a private note, in the same unified timeline as messages and calls. It is never
 * sent to the contact - the backend writes it to thread_notes, not to messages. */
export interface NoteTimelineItem {
  kind: "note";
  id: string;
  author_name: string;
  body: string;
  mentions: NoteMention[];
  occurred_at: string;
}

export type TimelineItem =
  | MessageTimelineItem
  | CallTimelineItem
  | VoicemailTimelineItem
  | NoteTimelineItem;

export interface InboxGrant {
  grantee_type: "department" | "user";
  grantee_id: string;
  role: "member" | "viewer";
}

export interface Department {
  id: string;
  name: string;
  is_active: boolean;
  // Matches backend/app/api/routes/departments.py DepartmentOut.member_user_ids.
  member_user_ids: string[];
}

export interface OrgMember {
  user_id: string;
  full_name: string;
  email: string;
  role_name: string;
}

function queryString(params: Record<string, string | undefined | null>): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  });
  const qs = query.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchInboxes(api: ApiClient): Promise<Inbox[]> {
  return api.request<Inbox[]>("/api/v1/inboxes");
}

/** P26 widened this: the same PATCH now carries the two reply/resolve targets. Sending
 * `sla_first_response_minutes: null` does NOT clear it (the backend reads null as "not
 * supplied") - pass `clear_sla_first_response: true` instead. */
export async function patchInbox(
  api: ApiClient,
  id: string,
  data: {
    name?: string;
    color?: string;
    sla_first_response_minutes?: number;
    sla_resolution_minutes?: number;
    clear_sla_first_response?: boolean;
    clear_sla_resolution?: boolean;
  },
): Promise<Inbox> {
  return api.request<Inbox>(`/api/v1/inboxes/${id}`, { method: "PATCH", json: data });
}

export async function fetchInboxGrants(api: ApiClient, id: string): Promise<InboxGrant[]> {
  return api.request<InboxGrant[]>(`/api/v1/inboxes/${id}/grants`);
}

export async function putInboxGrants(
  api: ApiClient,
  id: string,
  grants: InboxGrant[],
): Promise<InboxGrant[]> {
  return api.request<InboxGrant[]>(`/api/v1/inboxes/${id}/grants`, {
    method: "PUT",
    json: { grants },
  });
}

export async function fetchDepartments(api: ApiClient): Promise<Department[]> {
  return api.request<Department[]>("/api/v1/departments");
}

export async function createDepartment(
  api: ApiClient,
  data: { name: string; is_active?: boolean },
): Promise<Department> {
  return api.request<Department>("/api/v1/departments", { method: "POST", json: data });
}

export async function patchDepartment(
  api: ApiClient,
  id: string,
  data: { name?: string; is_active?: boolean },
): Promise<Department> {
  return api.request<Department>(`/api/v1/departments/${id}`, { method: "PATCH", json: data });
}

export async function deleteDepartment(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/departments/${id}`, { method: "DELETE" });
}

export async function putDepartmentMembers(
  api: ApiClient,
  id: string,
  user_ids: string[],
): Promise<Department> {
  return api.request<Department>(`/api/v1/departments/${id}/members`, {
    method: "PUT",
    json: { user_ids },
  });
}

export async function fetchOrgMembers(api: ApiClient): Promise<OrgMember[]> {
  // Matches the existing useOrgMembers() route in api/hooks.ts - "/api/v1/org/members"
  // (what the draft called) does not exist.
  return api.request<OrgMember[]>("/api/v1/orgs/current/members");
}

export interface FetchConversationsParams {
  inbox_id?: string;
  tab?: ConversationTab;
  filter?: ConversationFilter;
  q?: string;
  cursor?: string;
}

export async function fetchConversations(
  api: ApiClient,
  params: FetchConversationsParams,
): Promise<CursorPage<Conversation>> {
  return api.request<CursorPage<Conversation>>(
    `/api/v1/conversations${queryString({
      inbox_id: params.inbox_id,
      tab: params.tab,
      filter: params.filter,
      q: params.q,
      cursor: params.cursor,
    })}`,
  );
}

/** The API has NO per-inbox unread count endpoint, so this is a CLIENT-SIDE tally over
 * the FIRST page of unread conversations only; when `truncated` is true the displayed
 * counts are a floor, not a total, and the UI must say so rather than lie. Do not loop
 * over pages.
 */
export async function fetchUnreadByInbox(api: ApiClient): Promise<{
  counts: Record<string, number>;
  truncated: boolean;
}> {
  const page = await fetchConversations(api, { filter: "unread" });
  const counts: Record<string, number> = {};

  for (const item of page.items) {
    if (!item.inbox_id) continue;
    counts[item.inbox_id] = (counts[item.inbox_id] ?? 0) + 1;
  }

  return { counts, truncated: page.next_cursor != null };
}

export async function fetchConversationTimeline(
  api: ApiClient,
  contactE164: string,
  ourE164: string,
  cursor?: string,
): Promise<CursorPage<TimelineItem>> {
  return api.request<CursorPage<TimelineItem>>(
    `/api/v1/conversations/${encodeURIComponent(contactE164)}/timeline${queryString({
      our_e164: ourE164,
      cursor,
    })}`,
  );
}

export async function fetchContact(api: ApiClient, id: string): Promise<Contact> {
  return api.request<Contact>(`/api/v1/contacts/${id}`);
}

export async function updateContact(
  api: ApiClient,
  id: string,
  data: Partial<Omit<Contact, "id" | "phones">>,
): Promise<Contact> {
  return api.request<Contact>(`/api/v1/contacts/${id}`, { method: "PATCH", json: data });
}

/** The backend PATCH replaces `attributes` wholesale (patch_contact() assigns
 * `contact.attributes = validate_attributes(payload.attributes)` rather than merging),
 * so a single-field edit must send the FULL merged attributes object or it would wipe
 * out every other attribute (including custom fields defined elsewhere). `currentAttributes`
 * should be the caller's latest known `Contact.attributes` (e.g. from the cached
 * fetchContact() result) so the merge is against real data, not a stale default. */
export async function updateContactAttributes(
  api: ApiClient,
  id: string,
  currentAttributes: ContactAttributes,
  patch: Partial<ContactAttributes>,
): Promise<Contact> {
  return updateContact(api, id, { attributes: { ...currentAttributes, ...patch } });
}

// ----------------------------------------------------------------------------------
// Notes (item 3) - a proper list via GET/POST /api/v1/contacts/{id}/notes, replacing
// the single free-text `Contact.notes` field ContactPanel used to PATCH (a field
// backend/app/api/routes/contacts.py's ContactPatch schema never actually accepted, so
// those saves silently no-op'd).
// ----------------------------------------------------------------------------------
export interface ContactNote {
  id: string;
  body: string;
  // Item 7: backend add_note only returns {id, body, created_at} - author_user_id is not
  // sent back on creation, only on later GETs of the full note list.
  author_user_id?: string | null;
  created_at: string;
}

export async function fetchContactNotes(api: ApiClient, contactId: string): Promise<ContactNote[]> {
  return api.request<ContactNote[]>(`/api/v1/contacts/${contactId}/notes`);
}

export async function addContactNote(
  api: ApiClient,
  contactId: string,
  body: string,
): Promise<ContactNote> {
  return api.request<ContactNote>(`/api/v1/contacts/${contactId}/notes`, {
    method: "POST",
    json: { body },
  });
}

export async function patchThread(
  api: ApiClient,
  threadId: string,
  status: "open" | "closed",
): Promise<void> {
  await api.request<void>(`/api/v1/threads/${threadId}`, {
    method: "PATCH",
    json: { status },
  });
}

export async function putImportantPair(
  api: ApiClient,
  ourE164: string,
  contactE164: string,
  important: boolean,
): Promise<void> {
  await api.request<void>("/api/v1/inbox/important-pair", {
    method: "POST",
    json: { our_e164: ourE164, contact_e164: contactE164, important },
  });
}
