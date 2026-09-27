/**
 * One-off invoices for a workspace. Backend: the ops console invoice routes (build, price,
 * charge, retry, void) and the customer billing invoices route.
 *
 * The server owns pricing: a preview is a request, never local arithmetic, and every write
 * refetches the org's list. A charge needs an admin operator; a reviewer gets a 403 that we
 * surface verbatim rather than pretending the change stuck.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

export type InvoicePackage = "sms" | "mms" | "voice";

export type InvoiceLineType = "package" | "credit" | "item" | "discount";

export interface InvoiceLine {
  type: InvoiceLineType;
  description: string;
  amount_cents: number;
  package?: InvoicePackage;
  quantity?: number;
  units?: number;
}

export type InvoiceState = "pending" | "paid" | "failed" | "void";

/** charge_card = the card on file right away; email_link = Stripe emails a pay link. */
export type InvoiceCollection = "charge_card" | "email_link";

export interface CustomInvoice {
  id: string;
  state: InvoiceState;
  number: string | null;
  memo: string | null;
  lines: InvoiceLine[];
  list_micros: number;
  discount_micros: number;
  total_micros: number;
  paid_micros: number;
  error: string | null;
  collection: InvoiceCollection;
  emailed_to: string | null;
  hosted_invoice_url: string | null;
  invoice_pdf: string | null;
  created_at: string | null;
  paid_at: string | null;
}

/** What the operator's form sends. Prices the operator does not type are left off so the
 * server prices them at list, exactly like it does for a real subscription change. */
export type InvoiceLineInput =
  | {
      type: "package";
      package: InvoicePackage;
      quantity: number;
      amount_cents?: number;
      description?: string;
    }
  | { type: "credit"; amount_cents: number; description?: string }
  | { type: "item"; description: string; amount_cents: number }
  | {
      type: "discount";
      percent?: number;
      amount_cents?: number;
      description?: string;
    };

export interface InvoiceIn {
  lines: InvoiceLineInput[];
  memo?: string;
  collection?: InvoiceCollection;
  email?: string;
  days_until_due?: number;
}

/** What the server says the current form costs, in micros (1/1,000,000 dollar). `paid` is the
 * total that would be charged (list minus discount). */
export interface InvoicePreview {
  lines: InvoiceLine[];
  list: number;
  discount: number;
  paid: number;
}

/** Operator-facing package labels, in the order the package select shows them. */
export const INVOICE_PACKAGES: InvoicePackage[] = ["sms", "mms", "voice"];

export const INVOICE_PACKAGE_LABELS: Record<InvoicePackage, string> = {
  sms: "SMS",
  mms: "MMS",
  voice: "Call minutes",
};

function orgInvoicesPath(orgId: string): string {
  return `/api/v1/ops/console/orgs/${orgId}/invoices`;
}

// ---------------------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------------------

export async function fetchOrgInvoices(
  api: ApiClient,
  orgId: string,
): Promise<CustomInvoice[]> {
  const data = await api.request<{ invoices: CustomInvoice[] }>(
    orgInvoicesPath(orgId),
  );
  return data.invoices ?? [];
}

export async function previewInvoice(
  api: ApiClient,
  orgId: string,
  body: InvoiceIn,
): Promise<InvoicePreview> {
  return api.request<InvoicePreview>(`${orgInvoicesPath(orgId)}/preview`, {
    method: "POST",
    json: body,
  });
}

export async function sendInvoice(
  api: ApiClient,
  orgId: string,
  body: InvoiceIn,
): Promise<CustomInvoice> {
  return api.request<CustomInvoice>(orgInvoicesPath(orgId), {
    method: "POST",
    json: body,
  });
}

export async function retryInvoice(
  api: ApiClient,
  orgId: string,
  invoiceId: string,
): Promise<CustomInvoice> {
  return api.request<CustomInvoice>(
    `${orgInvoicesPath(orgId)}/${invoiceId}/retry`,
    {
      method: "POST",
    },
  );
}

export async function voidInvoice(
  api: ApiClient,
  orgId: string,
  invoiceId: string,
): Promise<CustomInvoice> {
  return api.request<CustomInvoice>(
    `${orgInvoicesPath(orgId)}/${invoiceId}/void`,
    {
      method: "POST",
    },
  );
}

export async function fetchMyInvoices(
  api: ApiClient,
): Promise<CustomInvoice[]> {
  const data = await api.request<{ invoices: CustomInvoice[] }>(
    "/api/v1/billing/invoices",
  );
  return data.invoices ?? [];
}

// ---------------------------------------------------------------------------------------
// react-query hooks
// ---------------------------------------------------------------------------------------

/** Root of the query keys used by the console hooks in @/api/opsConsole (useConsoleOrgs et al).
 * An invoice write changes what the org row's billing looks like, so the org list is refetched
 * along with the invoice list. */
const CONSOLE_QUERY_ROOT = ["ops", "console"] as const;

export function orgInvoicesQueryKey(orgId: string) {
  return [...CONSOLE_QUERY_ROOT, "org-invoices", orgId] as const;
}

export function myInvoicesQueryKey() {
  return ["billing", "invoices"] as const;
}

function invalidateOrgInvoices(
  qc: ReturnType<typeof useQueryClient>,
  orgId: string,
) {
  void qc.invalidateQueries({ queryKey: orgInvoicesQueryKey(orgId) });
  void qc.invalidateQueries({ queryKey: CONSOLE_QUERY_ROOT });
}

/** The workspace's owners: the only addresses a pay link may be emailed to. */
export function useOrgInvoiceOwners(api: ApiClient, orgId: string) {
  return useQuery({
    queryKey: [...orgInvoicesQueryKey(orgId), "owners"] as const,
    queryFn: async () =>
      (await api.request<{ owner_emails?: string[] }>(orgInvoicesPath(orgId)))
        .owner_emails ?? [],
  });
}

export function useOrgInvoices(api: ApiClient, orgId: string) {
  return useQuery({
    queryKey: orgInvoicesQueryKey(orgId),
    queryFn: () => fetchOrgInvoices(api, orgId),
  });
}

/** Pricing the draft form is a read as far as the invoice list is concerned, so a preview does
 * not invalidate anything. */
export function usePreviewInvoice(api: ApiClient, orgId: string) {
  return useMutation({
    mutationFn: (body: InvoiceIn) => previewInvoice(api, orgId, body),
  });
}

export function useSendInvoice(api: ApiClient, orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: InvoiceIn) => sendInvoice(api, orgId, body),
    onSuccess: () => invalidateOrgInvoices(qc, orgId),
  });
}

export function useRetryInvoice(api: ApiClient, orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (invoiceId: string) => retryInvoice(api, orgId, invoiceId),
    onSuccess: () => invalidateOrgInvoices(qc, orgId),
  });
}

export function useVoidInvoice(api: ApiClient, orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (invoiceId: string) => voidInvoice(api, orgId, invoiceId),
    onSuccess: () => invalidateOrgInvoices(qc, orgId),
  });
}

export function useMyInvoices(api: ApiClient) {
  return useQuery({
    queryKey: myInvoicesQueryKey(),
    queryFn: () => fetchMyInvoices(api),
  });
}

// ---------------------------------------------------------------------------------------
// Small display helpers
// ---------------------------------------------------------------------------------------

/** Cents the backend prices individual lines in -> the dollars the operator sees. */
export function formatCents(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`;
}
