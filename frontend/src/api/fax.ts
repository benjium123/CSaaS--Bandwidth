/**
 * Fax: send and list org faxes, download a fax's document, and switch a number's fax mode
 * on or off.
 *
 * Mirrors backend/app/api/routes/fax.py: GET/POST /api/v1/fax, GET
 * /api/v1/fax/{id}/document, and PATCH /api/v1/numbers/{id}/fax-mode.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthedBlob, type ApiClient } from "./client";

export type FaxDirection = "inbound" | "outbound";

/** In-flight statuses; a fax in any of these keeps the list polling (see FAX_ACTIVE_STATUSES
 * below). Everything else (delivered, received, failed) is terminal. */
export type FaxStatus =
  | "queued"
  | "sending"
  | "receiving"
  | "delivered"
  | "received"
  | "failed";

export interface FaxOut {
  id: string;
  direction: FaxDirection;
  status: FaxStatus | string;
  from: string;
  to: string;
  pages: number | null;
  charged_micros: number;
  failure_reason: string | null;
  has_document: boolean;
  document_name: string | null;
  created_at: string | null;
  completed_at: string | null;
}

export interface FaxNumberOut {
  id: string;
  e164: string;
  carrier: string;
  fax_mode: boolean;
}

export interface FaxListOut {
  faxes: FaxOut[];
  /** e164s currently eligible to send from - i.e. numbers[].fax_mode === true. */
  fax_numbers: string[];
  numbers: FaxNumberOut[];
}

const FAX_ACTIVE_STATUSES = new Set(["queued", "sending", "receiving"]);

export const FAX_QUERY_KEY = ["fax"] as const;
const FAX_POLL_MS = 10000;

/** Polls every 10s while any listed fax is still queued/sending/receiving, and stops once
 * every fax has settled - mirrors `useCalls` in api/hooks.ts. */
export function useFaxes(api: ApiClient) {
  return useQuery({
    queryKey: FAX_QUERY_KEY,
    queryFn: () => api.request<FaxListOut>("/api/v1/fax"),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      return data.faxes.some((f) => FAX_ACTIVE_STATUSES.has(f.status)) ? FAX_POLL_MS : false;
    },
  });
}

export const MAX_FAX_BYTES = 20_000_000;
export const ALLOWED_FAX_TYPES: readonly string[] = ["application/pdf", "image/tiff"];

/** Client-side mirror of the server's own checks (fax_svc.MAX_BYTES / ALLOWED_TYPES) - never
 * the source of truth, just a faster no for the obviously-wrong file. */
export function faxFileRejectionReason(file: { type: string; size: number; name: string }): string | null {
  if (file.size > MAX_FAX_BYTES) return "Faxes can be up to 20 MB.";
  const name = file.name.toLowerCase();
  const looksRight =
    ALLOWED_FAX_TYPES.includes(file.type) || name.endsWith(".pdf") || name.endsWith(".tif") || name.endsWith(".tiff");
  if (!looksRight) return "Attach a PDF or TIFF.";
  return null;
}

export function useSendFax(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { from: string; to: string; file: File }) => {
      const form = new FormData();
      // Do not pass `json`; FormData must go through `body` so the browser supplies the
      // multipart boundary (see api/messaging.ts uploadMedia).
      form.append("from", vars.from);
      form.append("to", vars.to);
      form.append("file", vars.file);
      return api.request<FaxOut>("/api/v1/fax", { method: "POST", body: form });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: FAX_QUERY_KEY });
    },
  });
}

export function useSetFaxMode(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { numberId: string; enabled: boolean }) =>
      api.request<{ id: string; e164: string; fax_mode: boolean }>(
        `/api/v1/numbers/${vars.numberId}/fax-mode`,
        { method: "PATCH", json: { enabled: vars.enabled } },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: FAX_QUERY_KEY });
    },
  });
}

/** The document needs the auth headers, so it cannot simply be an <a href> the browser
 * follows - same reasoning as downloadContactExport in api/contactsPro.ts. */
export async function downloadFaxDocument(api: ApiClient, faxId: string, filename: string): Promise<void> {
  const blob = await fetchAuthedBlob(api, `/api/v1/fax/${faxId}/document`);
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
