/**
 * Contacts domain (Phase 22).
 *
 * The generated types predate owner/department fields, so this module owns the
 * client-side ContactOut shape and every mutation that touches the new contact
 * ownership endpoints. It lives in the ["contacts"] key family on purpose: the
 * older useContacts in api/hooks.ts also keys its list under ["contacts"], so
 * one invalidateQueries({ queryKey: ["contacts"] }) refreshes both this page and
 * NewConversationPanel.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

export type ContactPhone = {
  e164: string;
  label?: string | null;
  is_primary?: boolean;
};

export type ContactOut = {
  id: string;
  display_name: string;
  first_name?: string | null;
  last_name?: string | null;
  company_id?: string | null;
  attributes?: Record<string, unknown>;
  phones: ContactPhone[];
  created_at?: string;
  owner_user_id: string | null;
  department_id: string | null;
};

export type ContactFilter = "mine" | "team" | "unowned";

export const CONTACTS_QUERY_KEY = ["contacts"] as const;

export function useContacts(
  api: ApiClient,
  params: { q?: string; filter?: ContactFilter | null },
) {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.filter) query.set("filter", params.filter);
  const qs = query.toString();

  return useQuery({
    queryKey: ["contacts", { q: params.q ?? "", filter: params.filter ?? null }],
    queryFn: () => api.request<ContactOut[]>(`/api/v1/contacts${qs ? `?${qs}` : ""}`),
  });
}

export function useCreateContact(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { display_name: string; phones: ContactPhone[] }) =>
      api.request<ContactOut>("/api/v1/contacts", { method: "POST", json: vars }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
    },
  });
}

export function useAssignContactOwner(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      contactId: string;
      owner_user_id?: string | null;
      department_id?: string | null;
    }) => {
      const { contactId, owner_user_id = null, department_id = null } = vars;
      return api.request<ContactOut>(`/api/v1/contacts/${contactId}/owner`, {
        method: "PATCH",
        json: { owner_user_id, department_id },
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
    },
  });
}

export function useBulkAssignContacts(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      contact_ids: string[];
      owner_user_id?: string | null;
      department_id?: string | null;
    }) =>
      api.request<{ updated: number }>("/api/v1/contacts/bulk/assign", {
        method: "POST",
        json: {
          contact_ids: vars.contact_ids,
          owner_user_id: vars.owner_user_id ?? null,
          department_id: vars.department_id ?? null,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
    },
  });
}

export type ContactVisibility = "everyone" | "department" | "owner";

export type OrgOut = {
  contact_visibility: ContactVisibility;
};

export function useCurrentOrg(api: ApiClient) {
  return useQuery({
    queryKey: ["org", "current"],
    queryFn: () => api.request<OrgOut>("/api/v1/orgs/current/settings"),
  });
}

export function useUpdateContactVisibility(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { contact_visibility: ContactVisibility }) =>
      api.request<OrgOut>("/api/v1/orgs/current/settings", { method: "PATCH", json: vars }),
    onSuccess: (data) => {
      qc.setQueryData(["org", "current"], data);
      // The policy changes which contact records the list endpoint returns.
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
    },
  });
}

export function getErrorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}
