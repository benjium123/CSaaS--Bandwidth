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
// NOTE: P11 added a second, differently-shaped "CampaignOut"/"CampaignIn" pair in
// app/api/routes/outbound.py. FastAPI's default schema naming disambiguates same-named
// models from different modules by qualifying them with the module path, so the bare
// "CampaignOut"/"CampaignIn" keys no longer exist in the generated schema - this alias
// (registration/10DLC campaigns, unrelated to P11 outbound campaigns) now points at the
// qualified name to match.
export type CampaignOut = components["schemas"]["app__api__routes__registration__CampaignOut"];
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

/** Thread AI state (plan DR-5): off | active | handed_off. `off` is the default and is
 * never set from the console - it only ever comes from an sms_enabled profile seeing the
 * thread for the first time. */
export type ThreadAiState = "off" | "active" | "handed_off";
export type ThreadAiOut = { id: string; ai_state: ThreadAiState };

export function useThreadAiState(api: ApiClient, threadId: string | null) {
  return useQuery({
    queryKey: ["thread-ai", threadId],
    queryFn: () => api.request<ThreadAiOut>(`/api/v1/threads/${threadId}/ai`),
    enabled: Boolean(threadId),
  });
}

/** The re-arm / take-over pair. Only "active" and "handed_off" are settable here. */
export function useSetThreadAiState(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { threadId: string; state: "active" | "handed_off" }) =>
      api.request<ThreadAiOut>(`/api/v1/threads/${vars.threadId}/ai`, {
        method: "POST",
        json: { state: vars.state },
      }),
    onSuccess: (data, vars) => qc.setQueryData(["thread-ai", vars.threadId], data),
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
  /** SMS agent (P10 DR-5/DR-7). */
  sms_enabled?: boolean;
  sms_turn_ceiling?: number;
  sms_handoff_keywords?: string[];
  sms_max_reply_chars?: number;
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

/* ---------------------------------------------------------------------------------------
 * Routing / providers (Providers page)
 * ------------------------------------------------------------------------------------- */

export type CarrierCatalogOut = components["schemas"]["CarrierCatalogOut"];
export type ProbeOut = components["schemas"]["ProbeOut"];
export type RoutingPolicyOut = components["schemas"]["PolicyOut"];
export type RoutingPolicyIn = components["schemas"]["PolicyIn"];

export function useCarrierCatalog(api: ApiClient) {
  return useQuery({
    queryKey: ["carrier-catalog"],
    queryFn: () => api.request<CarrierCatalogOut[]>("/api/v1/routing/catalog"),
  });
}

/** Operator-triggered only (never on boot) - see providers/probes.py. */
export function useProbeCarrier(api: ApiClient) {
  return useMutation({
    mutationFn: (name: string) =>
      api.request<ProbeOut>(`/api/v1/routing/carriers/${name}/probe`, { method: "POST" }),
  });
}

export function useRoutingPolicy(api: ApiClient) {
  return useQuery({
    queryKey: ["routing-policy"],
    queryFn: () => api.request<RoutingPolicyOut>("/api/v1/routing/policy"),
  });
}

export function useUpdateRoutingPolicy(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: RoutingPolicyIn) =>
      api.request<RoutingPolicyOut>("/api/v1/routing/policy", { method: "PATCH", json: vars }),
    onSuccess: (data) => qc.setQueryData(["routing-policy"], data),
  });
}

/* ---------------------------------------------------------------------------------------
 * Team: org members + invite-only registration
 * ------------------------------------------------------------------------------------- */

export type MemberOut = components["schemas"]["MemberOut"];
export type InviteOut = components["schemas"]["InviteOut"];
export type InviteCreatedOut = components["schemas"]["InviteCreatedOut"];

export function useOrgMembers(api: ApiClient) {
  return useQuery({
    queryKey: ["org-members"],
    queryFn: () => api.request<MemberOut[]>("/api/v1/orgs/current/members"),
  });
}

export function useInvites(api: ApiClient) {
  return useQuery({
    queryKey: ["org-invites"],
    queryFn: () => api.request<InviteOut[]>("/api/v1/orgs/current/invites"),
  });
}

export function useCreateInvite(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { email: string; role_name: string }) =>
      api.request<InviteCreatedOut>("/api/v1/orgs/current/invites", {
        method: "POST",
        json: vars,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["org-invites"] }),
  });
}

export function useRevokeInvite(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) =>
      api.request<InviteOut>(`/api/v1/orgs/current/invites/${inviteId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["org-invites"] }),
  });
}

/* ---------------------------------------------------------------------------------------
 * Outbound engine: contact lists + SMS/voice campaigns (Phase 11)
 * ------------------------------------------------------------------------------------- */

export type ListPreviewOut = components["schemas"]["ListPreviewOut"];
export type ListOut = components["schemas"]["ListOut"];
export type ListRowOut = components["schemas"]["ListRowOut"];
export type OutboundCampaignOut = components["schemas"]["app__api__routes__outbound__CampaignOut"];
export type OutboundProgressOut = components["schemas"]["ProgressOut"];

const LIST_POLL_MS = 3000;
const CAMPAIGN_PROGRESS_POLL_MS = 3000;

/** Lists poll only while at least one is still `importing` - a report artifact that
 * finishes on its own once the background import task completes (plan DR-8). */
export function useLists(api: ApiClient) {
  return useQuery({
    queryKey: ["outbound-lists"],
    queryFn: () => api.request<ListOut[]>("/api/v1/outbound/lists"),
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      const data = query.state.data;
      if (!data) return false;
      return data.some((l) => l.status === "importing") ? LIST_POLL_MS : false;
    },
  });
}

export function useList(api: ApiClient, listId: string | null) {
  return useQuery({
    queryKey: ["outbound-list", listId],
    queryFn: () => api.request<ListOut>(`/api/v1/outbound/lists/${listId}`),
    enabled: Boolean(listId),
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      return query.state.data?.status === "importing" ? LIST_POLL_MS : false;
    },
  });
}

/** The per-row import report (plan DR-8/DR-9), filterable by outcome status. */
export function useListRows(api: ApiClient, listId: string | null, status?: string) {
  const params = new URLSearchParams({ limit: "200" });
  if (status) params.set("status", status);
  return useQuery({
    queryKey: ["outbound-list-rows", listId, status ?? null],
    queryFn: () =>
      api.request<ListRowOut[]>(`/api/v1/outbound/lists/${listId}/rows?${params.toString()}`),
    enabled: Boolean(listId),
  });
}

/** Step 1 (DR-8): upload the file, get back headers + preview rows + a suggested
 * mapping. The list row already exists server-side (status "importing") - /commit
 * confirms the mapping and starts the background import. */
export function useUploadList(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { file: File; name?: string }) => {
      const form = new FormData();
      form.append("file", vars.file);
      if (vars.name) form.append("name", vars.name);
      // No `json` - the client only sets Content-Type when `json` is passed, so the
      // browser adds the correct multipart boundary header for this FormData body.
      return api.request<ListPreviewOut>("/api/v1/outbound/lists", { method: "POST", body: form });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["outbound-lists"] }),
  });
}

export function useCommitList(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { listId: string; mapping: Record<string, string> }) =>
      api.request<ListOut>(`/api/v1/outbound/lists/${vars.listId}/commit`, {
        method: "POST",
        json: { mapping: vars.mapping },
      }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["outbound-lists"] });
      qc.setQueryData(["outbound-list", data.id], data);
    },
  });
}

export type OutboundCampaignFilters = { channel?: string; status?: string };

export function useOutboundCampaigns(api: ApiClient, filters: OutboundCampaignFilters = {}) {
  const params = new URLSearchParams();
  if (filters.channel) params.set("channel", filters.channel);
  if (filters.status) params.set("status", filters.status);
  const qs = params.toString();

  return useQuery({
    queryKey: ["outbound-campaigns", filters],
    queryFn: () =>
      api.request<OutboundCampaignOut[]>(`/api/v1/outbound/campaigns${qs ? `?${qs}` : ""}`),
  });
}

export function useOutboundCampaign(api: ApiClient, campaignId: string | null) {
  return useQuery({
    queryKey: ["outbound-campaign", campaignId],
    queryFn: () => api.request<OutboundCampaignOut>(`/api/v1/outbound/campaigns/${campaignId}`),
    enabled: Boolean(campaignId),
  });
}

export type CreateOutboundCampaignVars = {
  name: string;
  channel: "sms" | "voice";
  list_id: string;
  body?: string | null;
  from_numbers?: string[];
  rate_per_minute?: number;
  daily_cap?: number;
  respect_warmup?: boolean;
  dialer_mode?: string | null;
  parallel_lines?: number;
  local_presence?: boolean;
  max_attempts?: number;
};

export function useCreateOutboundCampaign(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: CreateOutboundCampaignVars) =>
      api.request<OutboundCampaignOut>("/api/v1/outbound/campaigns", {
        method: "POST",
        json: vars,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["outbound-campaigns"] }),
  });
}

function useOutboundCampaignAction(api: ApiClient, action: "start" | "pause" | "cancel") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (campaignId: string) =>
      api.request<OutboundCampaignOut>(`/api/v1/outbound/campaigns/${campaignId}/${action}`, {
        method: "POST",
      }),
    onSuccess: (data, campaignId) => {
      qc.setQueryData(["outbound-campaign", campaignId], data);
      qc.invalidateQueries({ queryKey: ["outbound-campaigns"] });
    },
  });
}

export function useStartOutboundCampaign(api: ApiClient) {
  return useOutboundCampaignAction(api, "start");
}

export function usePauseOutboundCampaign(api: ApiClient) {
  return useOutboundCampaignAction(api, "pause");
}

export function useCancelOutboundCampaign(api: ApiClient) {
  return useOutboundCampaignAction(api, "cancel");
}

/** Polls only while the campaign is `running` - a completed/paused/cancelled campaign's
 * counts are done changing. */
export function useOutboundCampaignProgress(api: ApiClient, campaignId: string | null) {
  return useQuery({
    queryKey: ["outbound-campaign-progress", campaignId],
    queryFn: () =>
      api.request<OutboundProgressOut>(`/api/v1/outbound/campaigns/${campaignId}/progress`),
    enabled: Boolean(campaignId),
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      return query.state.data?.status === "running" ? CAMPAIGN_PROGRESS_POLL_MS : false;
    },
  });
}
