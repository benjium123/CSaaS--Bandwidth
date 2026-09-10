/**
 * Contacts pro (Phase 27): saved views, CSV export jobs, duplicate merge, erasure,
 * a single person's data bundle, and the workspace retention policy.
 *
 * One module owns every P27 path and shape so the Contacts page, the contact drawer and
 * the Settings → Workspace → Data panel all agree on them. Anything here that changes a
 * contact invalidates the shared ["contacts"] key family (api/contacts.ts owns it), so
 * the contacts list and NewConversationPanel refresh from one call.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthedBlob, type ApiClient } from "./client";
import { CONTACTS_QUERY_KEY, type ContactOut } from "./contacts";

// ----------------------------------------------------------------------------------
// Shapes (mirrors backend/app/api/routes/contacts.py and orgs.py)
// ----------------------------------------------------------------------------------

/** The only two keys POST /contacts/export accepts; anything else is a 422 server-side. */
export interface ContactExportFilters {
  q?: string | null;
  scope?: string | null;
}

export type ExportStatus = "running" | "done" | "failed";

export interface ExportJob {
  job_id: string;
  status: ExportStatus;
  rows: number;
  error: string | null;
  download_url: string | null;
}

export interface SavedView {
  id: string;
  name: string;
  filters: Record<string, unknown>;
  sort: string | null;
  /** true = the whole workspace sees it; false = private to the caller. */
  shared: boolean;
  created_at: string;
}

export interface SavedViewIn {
  name: string;
  filters: Record<string, unknown>;
  sort?: string | null;
  shared: boolean;
}

/** `shared` is deliberately absent: the server does not let a view change ownership. */
export interface SavedViewPatch {
  name?: string;
  filters?: Record<string, unknown>;
  sort?: string | null;
}

export type DuplicateReason = "name_and_email" | "phone";

export interface DuplicateCandidate {
  contact_id: string;
  display_name: string;
  phones: string[];
  /** Left as `string`: the sweeper may learn new reasons before this client does. */
  reason: string;
}

export function duplicateReasonLabel(reason: string): string {
  if (reason === "name_and_email") return "Same name and email";
  if (reason === "phone") return "Shares a phone number";
  return "Possible duplicate";
}

export interface MergeCounts {
  phones_moved: number;
  threads_moved: number;
  notes_moved: number;
  tags_moved: number;
  list_rows_moved: number;
}

export interface MergeResult {
  contact: ContactOut;
  merged: MergeCounts;
}

export interface ErasureResult {
  id: string;
  status: string;
  message: string;
}

export interface MyDataBundle {
  contact_id: string;
  bundle: Record<string, unknown>;
  csv: string;
}

export interface RetentionPolicy {
  messages_days: number | null;
  recordings_days: number | null;
  transcripts_days: number | null;
  imports_days: number | null;
}

export type RetentionPatch = Partial<RetentionPolicy>;

/** What a workspace that has never touched the page is actually running (backend
 * services/retention.py RETENTION_DEFAULTS) - the Data panel shows these as the
 * "unless you change it" numbers. */
export const RETENTION_DEFAULTS: RetentionPolicy = {
  messages_days: null,
  recordings_days: 90,
  transcripts_days: 365,
  imports_days: 30,
};

// ----------------------------------------------------------------------------------
// Query keys
// ----------------------------------------------------------------------------------
export const SAVED_VIEWS_KEY = ["contacts", "views"] as const;
export const RETENTION_KEY = ["org", "retention"] as const;

export function contactDuplicatesKey(contactId: string) {
  return ["contacts", contactId, "duplicates"] as const;
}

export function exportJobKey(jobId: string) {
  return ["contacts", "export", jobId] as const;
}

// ----------------------------------------------------------------------------------
// Saved views
// ----------------------------------------------------------------------------------
export function useSavedViews(api: ApiClient) {
  return useQuery({
    queryKey: SAVED_VIEWS_KEY,
    queryFn: () => api.request<SavedView[]>("/api/v1/contacts/views"),
  });
}

export function useCreateSavedView(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: SavedViewIn) =>
      api.request<SavedView>("/api/v1/contacts/views", { method: "POST", json: input }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SAVED_VIEWS_KEY });
    },
  });
}

export function useUpdateSavedView(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ viewId, ...patch }: { viewId: string } & SavedViewPatch) =>
      api.request<SavedView>(`/api/v1/contacts/views/${viewId}`, {
        method: "PATCH",
        json: patch,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SAVED_VIEWS_KEY });
    },
  });
}

export function useDeleteSavedView(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ viewId }: { viewId: string }) =>
      api.request<void>(`/api/v1/contacts/views/${viewId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SAVED_VIEWS_KEY });
    },
  });
}

// ----------------------------------------------------------------------------------
// Duplicates and merge
// ----------------------------------------------------------------------------------
export function useContactDuplicates(api: ApiClient, contactId: string | null) {
  return useQuery({
    // The key still varies by contact while disabled, so opening a second contact never
    // shows the first one's candidates for a frame.
    queryKey: contactDuplicatesKey(contactId ?? "none"),
    enabled: contactId !== null,
    queryFn: () =>
      api.request<DuplicateCandidate[]>(`/api/v1/contacts/${contactId}/duplicates`),
  });
}

export function useMergeContacts(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ contactId, loserIds }: { contactId: string; loserIds: string[] }) =>
      api.request<MergeResult>(`/api/v1/contacts/${contactId}/merge`, {
        method: "POST",
        json: { loser_ids: loserIds },
      }),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
      qc.invalidateQueries({ queryKey: contactDuplicatesKey(variables.contactId) });
    },
  });
}

// ----------------------------------------------------------------------------------
// Erasure and a single person's data
// ----------------------------------------------------------------------------------
export function useEraseContact(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ contactId }: { contactId: string }) =>
      api.request<ErasureResult>(`/api/v1/contacts/${contactId}/erase`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONTACTS_QUERY_KEY });
    },
  });
}

export function fetchMyDataBundle(api: ApiClient, contactId: string): Promise<MyDataBundle> {
  return api.request<MyDataBundle>(`/api/v1/contacts/${contactId}/export-my-data`);
}

// ----------------------------------------------------------------------------------
// Export jobs
// ----------------------------------------------------------------------------------
export function useStartContactExport(api: ApiClient) {
  return useMutation({
    mutationFn: (body: { filters: ContactExportFilters | null; view_id?: string | null }) =>
      api.request<{ job_id: string; status: string }>("/api/v1/contacts/export", {
        method: "POST",
        json: body,
      }),
  });
}

export function useExportJob(api: ApiClient, jobId: string | null) {
  return useQuery({
    queryKey: exportJobKey(jobId ?? "none"),
    enabled: jobId !== null,
    queryFn: () => api.request<ExportJob>(`/api/v1/contacts/export/${jobId}`),
    // The CSV is built in the background, so the only way to learn it is ready is to ask
    // again - and the only way not to poll a finished workspace forever is to stop once
    // the job settles.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "done" || status === "failed" ? false : 1500;
    },
  });
}

// ----------------------------------------------------------------------------------
// Retention policy (workspace-wide)
// ----------------------------------------------------------------------------------
export function useRetentionPolicy(api: ApiClient) {
  return useQuery({
    queryKey: RETENTION_KEY,
    queryFn: () => api.request<RetentionPolicy>("/api/v1/orgs/current/retention"),
  });
}

export function useUpdateRetentionPolicy(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    // Only the changed fields are sent: the server treats "absent" and "explicitly null"
    // as different answers ("leave it alone" vs "keep it forever").
    mutationFn: (patch: RetentionPatch) =>
      api.request<RetentionPolicy>("/api/v1/orgs/current/retention", {
        method: "PATCH",
        json: patch,
      }),
    onSuccess: (data) => {
      qc.setQueryData(RETENTION_KEY, data);
    },
  });
}

// ----------------------------------------------------------------------------------
// Downloads
// ----------------------------------------------------------------------------------
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking in the same tick can cancel the download in some browsers, so let the click
  // be handled first.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** The CSV needs the auth headers, so it cannot simply be an <a href> the browser follows. */
export async function downloadContactExport(api: ApiClient, jobId: string): Promise<void> {
  const blob = await fetchAuthedBlob(api, `/api/v1/contacts/export/${jobId}/download`);
  saveBlob(blob, "contacts.csv");
}

export function downloadTextFile(filename: string, text: string, mime: string): void {
  saveBlob(new Blob([text], { type: mime }), filename);
}
