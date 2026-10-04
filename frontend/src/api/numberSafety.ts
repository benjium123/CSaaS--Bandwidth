/**
 * Number safety: number portability checks and customer port-in requests, the ops-side port queue, and the
 * pending console grants queue.
 *
 * Mirrors backend/app/api/routes: /api/v1/ports*, /api/v1/ops/ports*, and
 * /api/v1/ops/console/grants*.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthedBlob, type ApiClient } from "./client";

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

/** One line in a port request's history. The API now sends a ready-made sentence in `text`;
 * the older at/status/note triple stays optional so anything still reading those keys keeps
 * working against a cached payload. */
export interface PortEvent {
  at?: string;
  status?: string;
  note?: string;
  text?: string;
  [k: string]: unknown;
}

/** The service address a transfer is filed against. */
export interface PortServiceAddress {
  street: string;
  extended: string;
  city: string;
  state: string;
  zip: string;
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
  /** Why the request needs the customer again, in plain language - shown in an amber box. */
  customer_reason: string | null;
  /** The customer may resubmit the details (PATCH) / stop the transfer (cancel). */
  can_edit: boolean;
  can_cancel: boolean;
  account_number: string | null;
  billing_number: string | null;
  service_address: PortServiceAddress | null;
  /** Port-out only: the provider the numbers are moving to. This is the *other* carrier, so
   * it may be shown to the customer. */
  gaining_carrier?: string | null;
  /** Port-out only: when ops must answer the request (ISO). */
  respond_by?: string | null;
  /** Port-out only: the owner has told us this transfer is not theirs. */
  disputed?: boolean;
  /** Port-out only: the signed-in customer may dispute a pending transfer. */
  can_dispute?: boolean;
}

export interface PortsOut {
  ports: PortRequest[];
  /** The workspace account id a new provider is asked for. */
  account_id?: string;
  /** True when the signed-in user owns/admins the workspace and may see the transfer PIN. */
  pin_holder?: boolean;
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

/** The part of a filed request a customer may change. Same shape as the create form minus
 * `numbers`/`carrier`, which are fixed once a transfer is filed. `pin` is optional because
 * an empty PIN means "keep the current one" - the key is then left out of the body entirely
 * (see `useUpdatePortIn`), which is unambiguous to the backend and to anyone reading the
 * request in a network tab. */
export interface PortInUpdate {
  authorized_name: string;
  business_name: string;
  account_number: string;
  pin?: string;
  billing_number: string;
  service_street: string;
  service_extended: string;
  service_city: string;
  service_state: string;
  service_zip: string;
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

/** PATCH /api/v1/ports/{id} - the customer fixing and resubmitting a filed request. Built the
 * same way as `useCreatePortIn`: FormData through `body`, and the two documents are only
 * appended when a replacement was actually picked (a null File would serialise as the string
 * "null"). */
export function useUpdatePortIn(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      id: string;
      form: PortInUpdate & { loa?: File | null; invoice?: File | null };
    }) => {
      const body = new FormData();
      body.append("authorized_name", vars.form.authorized_name);
      body.append("business_name", vars.form.business_name);
      body.append("account_number", vars.form.account_number);
      // Empty PIN = keep the current one, so the key is omitted rather than sent blank.
      if (vars.form.pin) body.append("pin", vars.form.pin);
      body.append("billing_number", vars.form.billing_number);
      body.append("service_street", vars.form.service_street);
      body.append("service_extended", vars.form.service_extended);
      body.append("service_city", vars.form.service_city);
      body.append("service_state", vars.form.service_state);
      body.append("service_zip", vars.form.service_zip);
      if (vars.form.loa) body.append("loa", vars.form.loa);
      if (vars.form.invoice) body.append("invoice", vars.form.invoice);
      return api.request<PortRequest>(`/api/v1/ports/${vars.id}`, { method: "PATCH", body });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PORTS_QUERY_KEY });
    },
  });
}

/** POST /api/v1/ports/{id}/cancel - stops a transfer that has not been filed yet. A 409
 * carries the API's own explanation and is surfaced verbatim by the caller. */
export function useCancelPort(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<PortRequest>(`/api/v1/ports/${id}/cancel`, { method: "POST" }),
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
/* Workspace transfer PIN (customer, owner/admin only)                        */
/* ------------------------------------------------------------------------- */

/** GET /api/v1/ports/pin. `pin` is only ever present on the reveal/rotate responses - the
 * plain GET carries no digits. */
export interface PortPinStatus {
  account_id: string;
  has_pin: boolean;
  numbers_total: number;
  numbers_protected: number;
  rotated_at: string | null;
  /** Only on the reveal/rotate responses. */
  pin?: string;
}

export const PORT_PIN_QUERY_KEY = ["port-pin"] as const;

export function usePortPin(api: ApiClient, enabled: boolean) {
  return useQuery({
    queryKey: PORT_PIN_QUERY_KEY,
    queryFn: () => api.request<PortPinStatus>("/api/v1/ports/pin"),
    enabled,
  });
}

/** POST /api/v1/ports/pin/reveal. May fail with code "step_up_required": the global
 * StepUpDialog opens for that, and the caller just shows the message inline so the button
 * can be pressed again afterwards. */
export function useRevealPortPin(api: ApiClient) {
  return useMutation({
    mutationFn: () => api.request<PortPinStatus>("/api/v1/ports/pin/reveal", { method: "POST" }),
  });
}

export function useRotatePortPin(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.request<PortPinStatus>("/api/v1/ports/pin/rotate", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PORT_PIN_QUERY_KEY });
    },
  });
}

/** POST /api/v1/ports/{id}/dispute - the owner telling us a pending port-out is not theirs. */
export function useDisputePortOut(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.request<PortRequest>(`/api/v1/ports/${id}/dispute`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PORTS_QUERY_KEY });
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
/* Port-out decisions (ops)                                                   */
/* ------------------------------------------------------------------------- */

/** One reason code ops can reject a port-out with. `reason_required` codes also need a
 * free-text explanation. */
export interface PortOutRejectionCode {
  code: number;
  label: string;
  reason_required: boolean;
}

export interface PortOutRejectionCodesOut {
  codes: PortOutRejectionCode[];
}

export const PORT_OUT_CODES_QUERY_KEY = ["ops-port-out-codes"] as const;

export function usePortOutCodes(api: ApiClient, id: string, enabled: boolean) {
  return useQuery({
    queryKey: [...PORT_OUT_CODES_QUERY_KEY, id],
    queryFn: () =>
      api.request<PortOutRejectionCodesOut>(
        `/api/v1/ops/ports/${id}/port-out/rejection-codes`,
      ),
    enabled,
  });
}

/** POST /api/v1/ops/ports/{id}/port-out/authorize - releases the numbers to the new provider. */
export function useAuthorizePortOut(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; note?: string }) =>
      api.request<PortRequest>(`/api/v1/ops/ports/${vars.id}/port-out/authorize`, {
        method: "POST",
        json: { note: vars.note },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: OPS_PORTS_QUERY_KEY });
    },
  });
}

/** POST /api/v1/ops/ports/{id}/port-out/reject - refuses a transfer that is not legitimate. */
export function useRejectPortOut(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; code: number; reason?: string }) =>
      api.request<PortRequest>(`/api/v1/ops/ports/${vars.id}/port-out/reject`, {
        method: "POST",
        json: { code: vars.code, reason: vars.reason },
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
