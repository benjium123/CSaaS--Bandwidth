import type { ApiClient } from "./client";

export type InboxRole = "admin" | "member" | "viewer";

export interface Inbox {
  id: string;
  name: string;
  color: string;
  e164: string;
  number_id: string;
  my_role: InboxRole;
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
  thread_id: string;
  contact: ContactSummary | null;
  snippet: string | null;
  last_event_type: ConversationEventType;
  direction: "inbound" | "outbound" | "unknown";
  last_event_at: string;
  unread: number;
  status: "open" | "closed";
}

export type ConversationTab = "chats" | "calls";
export type ConversationFilter = "open" | "unread" | "unresponded" | "all";

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

export type TimelineItem = MessageTimelineItem | CallTimelineItem | VoicemailTimelineItem;

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

export async function patchInbox(
  api: ApiClient,
  id: string,
  data: { name?: string; color?: string },
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
