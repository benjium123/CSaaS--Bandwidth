/**
 * Server state + freshness.
 *
 * Freshness is isolated HERE on purpose (plan DR-6): P2 polls, and swapping to a push
 * transport later touches this file only. Polling pauses when the tab is hidden.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";
import type { components } from "./types.gen";

export type CallOut = components["schemas"]["CallOut"];
export type CallDetailOut = components["schemas"]["CallDetailOut"];
export type CallLegOut = components["schemas"]["CallLegOut"];
export type RecordingOut = components["schemas"]["RecordingOut"];
export type NumberOut = components["schemas"]["NumberOut"];
export type AvailableNumberOut = components["schemas"]["SearchOut"];
export type BrandOut = components["schemas"]["BrandOut"];
export type CampaignOut = components["schemas"]["CampaignOut"];
export type TollfreeOut = components["schemas"]["TfvOut"];
export type AgentProfileOut = components["schemas"]["ProfileOut"];
export type TranscriptSegmentOut = components["schemas"]["TranscriptSegmentOut"];

export type InboxItem = {
  thread: {
    id: string;
    our_e164: string;
    contact_e164: string;
    status: string;
    assigned_user_id: string | null;
    last_message_at: string | null;
  };
  last_message: {
    id: string;
    direction: string;
    body: string | null;
    status: string;
    created_at: string;
  } | null;
  unread: number;
  contact: { id: string; display_name: string } | null;
  assignee: { id: string; full_name: string } | null;
  labels: { id: string; name: string; color: string }[];
};

export type InboxPage = { items: InboxItem[]; next_cursor: string | null };

export type Message = {
  id: string;
  thread_id: string;
  direction: string;
  status: string;
  from_e164: string;
  to_e164: string;
  body: string | null;
  segment_count_est: number | null;
  segment_count_carrier: number | null;
  error_code: string | null;
  created_at: string;
};

export type InboxFilters = {
  status?: string;
  assigned?: string;
  q?: string;
  label_id?: string;
};

const INBOX_POLL_MS = 4000;
const THREAD_POLL_MS = 2500;

/** Pause polling while the tab is hidden — an unattended tab should cost nothing. */
function pollWhenVisible(ms: number) {
  return () => (document.visibilityState === "visible" ? ms : false);
}

export function inboxQueryKey(filters: InboxFilters) {
  return ["inbox", filters] as const;
}

export function useInbox(api: ApiClient, filters: InboxFilters, enabled = true) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.assigned) params.set("assigned", filters.assigned);
  if (filters.q) params.set("q", filters.q);
  if (filters.label_id) params.set("label_id", filters.label_id);
  const qs = params.toString();

  return useQuery({
    queryKey: inboxQueryKey(filters),
    queryFn: () => api.request<InboxPage>(`/api/v1/inbox/threads${qs ? `?${qs}` : ""}`),
    refetchInterval: pollWhenVisible(INBOX_POLL_MS),
    enabled,
  });
}

export function useThreadMessages(api: ApiClient, threadId: string | null) {
  return useQuery({
    queryKey: ["messages", threadId],
    queryFn: () => api.request<Message[]>(`/api/v1/messages?thread_id=${threadId}`),
    enabled: Boolean(threadId),
    refetchInterval: pollWhenVisible(THREAD_POLL_MS),
  });
}

export function useSendMessage(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { to: string; body: string; allow_reassign?: boolean }) =>
      api.request<Message>("/api/v1/messages", { method: "POST", json: vars }),
    onSuccess: (msg) => {
      qc.invalidateQueries({ queryKey: ["messages", msg.thread_id] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
    },
  });
}

export function useMarkRead(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (threadId: string) =>
      api.request<void>(`/api/v1/threads/${threadId}/read`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["inbox"] }),
  });
}

export function usePatchThread(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      threadId: string;
      status?: string;
      assigned_user_id?: string | null;
      clear_assignee?: boolean;
    }) => {
      const { threadId, ...body } = vars;
      return api.request(`/api/v1/threads/${threadId}`, { method: "PATCH", json: body });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["inbox"] }),
  });
}

export function useContacts(api: ApiClient, q = "") {
  return useQuery({
    queryKey: ["contacts", q],
    queryFn: () =>
      api.request<
        { id: string; display_name: string; phones: { e164: string }[] }[]
      >(`/api/v1/contacts${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  });
}

export function useTags(api: ApiClient) {
  return useQuery({
    queryKey: ["tags"],
    queryFn: () => api.request<{ id: string; name: string; color: string }[]>("/api/v1/tags"),
  });
}

export function useNumbers(api: ApiClient) {
  return useQuery({
    queryKey: ["numbers"],
    queryFn: () => api.request<NumberOut[]>("/api/v1/numbers"),
  });
}

/* ---------------------------------------------------------------------------------------
 * Calls (Phase 5)
 * ------------------------------------------------------------------------------------- */

const CALL_POLL_MS = 5000;

/** Mirrors TERMINAL_CALL_STATUSES in app/models/voice.py. */
const TERMINAL_CALL_STATUSES = new Set(["completed", "failed", "busy", "no_answer", "canceled"]);

export function isTerminalCallStatus(status: string): boolean {
  return TERMINAL_CALL_STATUSES.has(status);
}

export type CallFilters = {
  contact_e164?: string;
  status?: string;
  limit?: number;
  offset?: number;
};

export function callsQueryKey(filters: CallFilters) {
  return ["calls", filters] as const;
}

/** Poll only while the tab is visible AND at least one listed call is still in flight. */
export function useCalls(api: ApiClient, filters: CallFilters, enabled = true) {
  const params = new URLSearchParams();
  if (filters.contact_e164) params.set("contact_e164", filters.contact_e164);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  if (filters.offset != null) params.set("offset", String(filters.offset));
  const qs = params.toString();

  return useQuery({
    queryKey: callsQueryKey(filters),
    queryFn: () => api.request<CallOut[]>(`/api/v1/calls${qs ? `?${qs}` : ""}`),
    enabled,
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      const data = query.state.data;
      if (!data) return CALL_POLL_MS;
      return data.some((c) => !isTerminalCallStatus(c.status)) ? CALL_POLL_MS : false;
    },
  });
}

export function useCall(api: ApiClient, callId: string | null) {
  return useQuery({
    queryKey: ["call", callId],
    queryFn: () => api.request<CallDetailOut>(`/api/v1/calls/${callId}`),
    enabled: Boolean(callId),
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      const data = query.state.data;
      if (!data) return CALL_POLL_MS;
      return isTerminalCallStatus(data.status) ? false : CALL_POLL_MS;
    },
  });
}

export function usePlaceCall(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      to: string;
      from?: string;
      carrier?: string;
      machine_detection?: string;
      tag?: string;
    }) => api.request<CallDetailOut>("/api/v1/calls", { method: "POST", json: vars }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}

export function useTransferCall(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { callId: string; to: string }) =>
      api.request<CallDetailOut>(`/api/v1/calls/${vars.callId}/transfer`, {
        method: "POST",
        json: { to: vars.to },
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["call", vars.callId] });
      qc.invalidateQueries({ queryKey: ["calls"] });
    },
  });
}

export function useHangupCall(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (callId: string) =>
      api.request<CallDetailOut>(`/api/v1/calls/${callId}/hangup`, { method: "POST" }),
    onSuccess: (_data, callId) => {
      qc.invalidateQueries({ queryKey: ["call", callId] });
      qc.invalidateQueries({ queryKey: ["calls"] });
    },
  });
}

/* ---------------------------------------------------------------------------------------
 * Numbers (Phase 4 upgrade) + registration lookups
 * ------------------------------------------------------------------------------------- */

export type AvailableNumberFilters = {
  carrier?: string;
  area_code?: string;
  contains?: string;
  locality?: string;
  region?: string;
  number_type?: string;
  limit?: number;
};

export function useAvailableNumbers(api: ApiClient, filters: AvailableNumberFilters, enabled: boolean) {
  const params = new URLSearchParams();
  if (filters.carrier) params.set("carrier", filters.carrier);
  if (filters.area_code) params.set("area_code", filters.area_code);
  if (filters.contains) params.set("contains", filters.contains);
  if (filters.locality) params.set("locality", filters.locality);
  if (filters.region) params.set("region", filters.region);
  if (filters.number_type) params.set("number_type", filters.number_type);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  const qs = params.toString();

  return useQuery({
    queryKey: ["numbers-available", filters],
    queryFn: () => api.request<AvailableNumberOut[]>(`/api/v1/numbers/available${qs ? `?${qs}` : ""}`),
    enabled,
    retry: false,
  });
}

export function useOrderNumber(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { e164: string; carrier?: string; campaign_id?: string }) =>
      api.request<NumberOut>("/api/v1/numbers/order", { method: "POST", json: vars }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["numbers"] }),
  });
}

export function useReleaseNumber(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (numberId: string) =>
      api.request<NumberOut>(`/api/v1/numbers/${numberId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["numbers"] }),
  });
}

export function useAssignCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { numberId: string; campaign_id: string | null }) =>
      api.request<NumberOut>(`/api/v1/numbers/${vars.numberId}/campaign`, {
        method: "PATCH",
        json: { campaign_id: vars.campaign_id },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["numbers"] }),
  });
}

export function useBrands(api: ApiClient) {
  return useQuery({
    queryKey: ["brands"],
    queryFn: () => api.request<BrandOut[]>("/api/v1/registration/brands"),
  });
}

export function useCampaigns(api: ApiClient) {
  return useQuery({
    queryKey: ["campaigns"],
    queryFn: () => api.request<CampaignOut[]>("/api/v1/registration/campaigns"),
  });
}

export function useTollfreeVerifications(api: ApiClient) {
  return useQuery({
    queryKey: ["tollfree-verifications"],
    queryFn: () => api.request<TollfreeOut[]>("/api/v1/registration/tollfree"),
  });
}

/* ---------------------------------------------------------------------------------------
 * AI agent (Phase 8)
 * ------------------------------------------------------------------------------------- */

export type AgentProfileFields = {
  name: string;
  system_prompt?: string;
  greeting?: string;
  voice_id?: string;
  llm_provider?: string;
  llm_model?: string;
  voicemail_message?: string;
};

export function useAgentProfiles(api: ApiClient) {
  return useQuery({
    queryKey: ["agent-profiles"],
    queryFn: () => api.request<AgentProfileOut[]>("/api/v1/agent/profiles"),
  });
}

export function useCreateAgentProfile(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: AgentProfileFields) =>
      api.request<AgentProfileOut>("/api/v1/agent/profiles", { method: "POST", json: vars }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent-profiles"] }),
  });
}

export function useUpdateAgentProfile(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string } & Partial<AgentProfileFields>) => {
      const { id, ...body } = vars;
      return api.request<AgentProfileOut>(`/api/v1/agent/profiles/${id}`, {
        method: "PATCH",
        json: body,
      });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent-profiles"] }),
  });
}

export function useDeleteAgentProfile(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<void>(`/api/v1/agent/profiles/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent-profiles"] }),
  });
}

export function useSetDefaultAgentProfile(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<AgentProfileOut>(`/api/v1/agent/profiles/${id}/default`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent-profiles"] }),
  });
}

/** POST /api/v1/calls/{call_id}/agent - the P7 dispatch endpoint. agent_name="ai" is
 * the real P8 pipeline; "echo" is P7's loop-back test agent, unused by this console. */
export function useDispatchAgent(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { callId: string; agent_name?: string }) =>
      api.request<{ dispatched: string; room: string; id: string }>(
        `/api/v1/calls/${vars.callId}/agent`,
        { method: "POST", json: { agent_name: vars.agent_name ?? "ai" } },
      ),
    onSuccess: (_data, vars) => qc.invalidateQueries({ queryKey: ["call", vars.callId] }),
  });
}

/* ---------------------------------------------------------------------------------------
 * Appointments + knowledge base (Phase 9)
 * ------------------------------------------------------------------------------------- */

export type AppointmentOut = {
  id: string;
  call_id: string | null;
  contact_e164: string;
  raw_when: string;
  scheduled_for: string | null;
  notes: string;
  status: string;
  created_by: string;
};

export function useAppointments(api: ApiClient, status?: string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return useQuery({
    queryKey: ["appointments", status ?? null],
    queryFn: () => api.request<AppointmentOut[]>(`/api/v1/appointments${qs}`),
  });
}

export function useUpdateAppointment(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      id: string;
      status?: string;
      scheduled_for?: string;
      notes?: string;
    }) => {
      const { id, ...body } = vars;
      return api.request<AppointmentOut>(`/api/v1/appointments/${id}`, {
        method: "PATCH",
        json: body,
      });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["appointments"] }),
  });
}

export type KbDocumentOut = { id: string; title: string; source: string };
export type KbChunkOut = { seq: number; text: string };
export type KbDocumentDetailOut = KbDocumentOut & { chunks: KbChunkOut[] };

export function useKbDocuments(api: ApiClient) {
  return useQuery({
    queryKey: ["kb-documents"],
    queryFn: () => api.request<KbDocumentOut[]>("/api/v1/kb/documents"),
  });
}

export function useKbDocument(api: ApiClient, documentId: string | null) {
  return useQuery({
    queryKey: ["kb-document", documentId],
    queryFn: () => api.request<KbDocumentDetailOut>(`/api/v1/kb/documents/${documentId}`),
    enabled: Boolean(documentId),
  });
}

export function useCreateKbDocument(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { title: string; text: string }) =>
      api.request<KbDocumentOut>("/api/v1/kb/documents", { method: "POST", json: vars }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb-documents"] }),
  });
}

export function useDeleteKbDocument(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<void>(`/api/v1/kb/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb-documents"] }),
  });
}
