/**
 * Server state + freshness.
 *
 * Freshness is isolated HERE on purpose (plan DR-6): P2 polls, and swapping to a push
 * transport later touches this file only. Polling pauses when the tab is hidden.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

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
    queryFn: () =>
      api.request<{ id: string; e164: string; is_active: boolean }[]>("/api/v1/numbers"),
  });
}
