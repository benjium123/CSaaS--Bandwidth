/**
 * Number safety: E911 emergency addresses and their assignment to numbers, number
 * portability checks and customer port-in requests, the ops-side port queue, and the
 * pending console grants queue.
 *
 * Mirrors backend/app/api/routes: GET/POST /api/v1/e911/addresses, POST
 * /api/v1/e911/numbers/{id}/address, /api/v1/ports*, /api/v1/ops/ports*, and
 * /api/v1/ops/console/grants*.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchAuthedBlob, type ApiClient } from "./client";

/* ------------------------------------------------------------------------- */
/* E911                                                                       */
/* ------------------------------------------------------------------------- */

export interface EmergencyAddress {
  id: string;
  label: string;
  caller_name: string;
  line1: string;
  line2: string;
  city: string;
  state: string;
  postal_code: string;
  country: string;
  status: "pending" | "valid" | "invalid" | string;
  last_error: string | null;
}

export interface E911Number {
  id: string;
  e164: string;
  emergency_address_id: string | null;
  e911_status: "none" | "pending" | "active" | "failed" | string;
  e911_error: string | null;
}

export interface EmergencyAddressesOut {
  addresses: EmergencyAddress[];
  numbers: E911Number[];
}

export interface AddressIn {
  label?: string;
  caller_name: string;
  line1: string;
  line2?: string;
  city: string;
  state: string;
  postal_code: string;
  country?: string;
}

/** Carrier-shaped verification suggestion - keys vary by carrier (line1/street,
 * postal_code/zip, ...), so this stays loose on purpose. */
export type AddressSuggestion = Record<string, string>;

export const E911_QUERY_KEY = ["e911"] as const;

/** Pulls the suggestions out of a 422 `address_not_verified` ApiError; anything else (a
 * different error code, a non-ApiError) has no suggestions. */
export function addressSuggestions(err: unknown): AddressSuggestion[] {
  if (!(err instanceof ApiError)) return [];
  const apiErr = err as unknown as {
    code?: string;
    details?: { suggestions?: AddressSuggestion[] } | null;
  };
  if (apiErr.code !== "address_not_verified") return [];
  return apiErr.details?.suggestions ?? [];
}

export function useEmergencyAddresses(api: ApiClient) {
  return useQuery({
    queryKey: E911_QUERY_KEY,
    queryFn: () => api.request<EmergencyAddressesOut>("/api/v1/e911/addresses"),
  });
}

export function useCreateEmergencyAddress(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AddressIn) =>
      api.request<EmergencyAddress>("/api/v1/e911/addresses", { method: "POST", json: body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: E911_QUERY_KEY });
    },
  });
}

export function useAssignEmergencyAddress(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { numberId: string; addressId: string }) =>
      api.request<E911Number>(`/api/v1/e911/numbers/${vars.numberId}/address`, {
        method: "POST",
        json: { address_id: vars.addressId },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: E911_QUERY_KEY });
      qc.invalidateQueries({ queryKey: ["numbers"] });
    },
  });
}

/* ------------------------------------------------------------------------- */
/* Porting (customer)                                                         */
/* ------------------------------------------------------------------------- */

export interface PortCheckResult {
  phone_number: string;
  portable: boolean;
  reason: string | null;
  fast_portable: boolean;
}

export interface PortCheckOut {
  results: PortCheckResult[];
}

export interface PortEvent {
  at?: string;
  status?: string;
  note?: string;
  [k: string]: unknown;
}

export interface PortRequest {
  id: string;
  org_id: string;
  direction: "in" | "out";
  carrier: string;
  numbers: string[];
  status: string;
  foc_date: string | null;
  last_error: string | null;
  authorized_name: string | null;
  business_name: string | null;
  manual: boolean;
  events: PortEvent[];
  created_at: string | null;
}

export interface PortsOut {
  ports: PortRequest[];
}

/** Text fields are comma-separated/free-form as submitted; `loa` and `invoice` are the
 * scanned PDFs. The multipart FormData is built inside `useCreatePortIn` so callers just
 * hand over the form values. */
export interface PortInForm {
  numbers: string;
  carrier: "telnyx" | "signalwire";
  authorized_name: string;
  business_name: string;
  account_number: string;
  pin: string;
  billing_number: string;
  service_street: string;
  service_extended: string;
  service_city: string;
  service_state: string;
  service_zip: string;
  loa: File;
  invoice: File;
}

/** Statuses ops can set by hand via POST /api/v1/ops/ports/{id}/status. */
export type PortManualStatus =
  | "in_process"
  | "exception"
  | "foc_confirmed"
  | "ported"
  | "cancelled";

export const PORTS_QUERY_KEY = ["ports"] as const;

/** Non-terminal port statuses; while any listed port is in one of these, the customer list
 * keeps polling (see `usePorts`). */
export const PORT_OPEN_STATUSES: readonly string[] = [
  "awaiting_review",
  "submitted",
  "in_process",
  "exception",
  "foc_confirmed",
];

const PORTS_POLL_MS = 60000;

export function usePortabilityCheck(api: ApiClient) {
  return useMutation({
    mutationFn: (numbers: string[]) =>
      api.request<PortCheckOut>("/api/v1/ports/check", { method: "POST", json: { numbers } }),
  });
}

/** Polls every 60s while any port is still open, and stops once every port has settled -
 * mirrors `useFaxes`. */
export function usePorts(api: ApiClient) {
  return useQuery({
    queryKey: PORTS_QUERY_KEY,
    queryFn: () => api.request<PortsOut>("/api/v1/ports"),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      return data.ports.some((p) => PORT_OPEN_STATUSES.includes(p.status)) ? PORTS_POLL_MS : false;
    },
  });
}

export function useCreatePortIn(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: PortInForm) => {
      const form = new FormData();
      // Do not pass `json`; FormData must go through `body` so the browser supplies the
      // multipart boundary (see api/fax.ts useSendFax and api/messaging.ts uploadMedia).
      form.append("numbers", vars.numbers);
      form.append("carrier", vars.carrier);
      form.append("authorized_name", vars.authorized_name);
      form.append("business_name", vars.business_name);
      form.append("account_number", vars.account_number);
      form.append("pin", vars.pin);
      form.append("billing_number", vars.billing_number);
      form.append("service_street", vars.service_street);
      form.append("service_extended", vars.service_extended);
      form.append("service_city", vars.service_city);
      form.append("service_state", vars.service_state);
      form.append("service_zip", vars.service_zip);
      form.append("loa", vars.loa);
      form.append("invoice", vars.invoice);
      return api.request<PortRequest>("/api/v1/ports", { method: "POST", body: form });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PORTS_QUERY_KEY });
    },
  });
}

export function useSetPortLock(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { numberId: string; locked: boolean }) =>
      api.request<{ id: string; port_locked: boolean }>(`/api/v1/ports/lock/${vars.numberId}`, {
        method: "POST",
        json: { locked: vars.locked },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["numbers"] });
    },
  });
}

/* ------------------------------------------------------------------------- */
/* Porting (ops)                                                              */
/* ------------------------------------------------------------------------- */

export const OPS_PORTS_QUERY_KEY = ["ops-ports"] as const;

export function useOpsPorts(api: ApiClient, status?: string) {
  return useQuery({
    queryKey: [...OPS_PORTS_QUERY_KEY, status ?? "all"],
    queryFn: () => {
      const qs = status ? `?status=${encodeURIComponent(status)}` : "";
      return api.request<PortsOut>(`/api/v1/ops/ports${qs}`);
    },
  });
}

/** The LOA/invoice scans need the auth headers, so they cannot simply be an <a href> the
 * browser follows - same reasoning as downloadFaxDocument in api/fax.ts. */
export function fetchPortDocument(
  api: ApiClient,
  id: string,
  kind: "loa" | "invoice",
): Promise<Blob> {
  return fetchAuthedBlob(api, `/api/v1/ops/ports/${id}/documents/${kind}`);
}

export function useApprovePort(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<PortRequest>(`/api/v1/ops/ports/${id}/approve`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: OPS_PORTS_QUERY_KEY });
    },
  });
}

export function useRejectPort(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; reason: string }) =>
      api.request<PortRequest>(`/api/v1/ops/ports/${vars.id}/reject`, {
        method: "POST",
        json: { reason: vars.reason },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: OPS_PORTS_QUERY_KEY });
    },
  });
}

export function useSetPortStatus(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      id: string;
      status: PortManualStatus;
      foc_date?: string | null;
      note?: string;
    }) =>
      api.request<PortRequest>(`/api/v1/ops/ports/${vars.id}/status`, {
        method: "POST",
        json: { status: vars.status, foc_date: vars.foc_date, note: vars.note },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: OPS_PORTS_QUERY_KEY });
    },
  });
}

/* ------------------------------------------------------------------------- */
/* Pending grants                                                             */
/* ------------------------------------------------------------------------- */

export interface PendingGrant {
  id: string;
  org_id: string;
  requested_at: string | null;
  requested_by: string;
  type: "credit" | "bundle";
  amount_micros?: number;
  kind?: string;
  units?: number;
  note?: string;
}

export const PENDING_GRANTS_QUERY_KEY = ["ops-grants-pending"] as const;

export function usePendingGrants(api: ApiClient) {
  return useQuery({
    queryKey: PENDING_GRANTS_QUERY_KEY,
    queryFn: () => api.request<PendingGrant[]>("/api/v1/ops/console/grants/pending"),
  });
}

export function useDecideGrant(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; approve: boolean }) =>
      api.request<Record<string, unknown>>(`/api/v1/ops/console/grants/${vars.id}/decide`, {
        method: "POST",
        json: { approve: vars.approve },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PENDING_GRANTS_QUERY_KEY });
    },
  });
}
