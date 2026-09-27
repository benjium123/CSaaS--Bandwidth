import { useState, type ReactNode } from "react";

import { formatCredits } from "@/api/billing";
import {
  useMyInvoices,
  type CustomInvoice,
  type InvoiceLine,
  type InvoiceState,
} from "@/api/customInvoices";
import { getErrorMessage } from "@/api/spend";
import { useAuth } from "@/auth/AuthContext";
import { Button, EmptyState, Spinner } from "@/components/ui/primitives";

/** The receipts for the cards we charged. Collapsed by default: this sits under the billing
 * settings next to the ledger, and most visits are about the balance, not a past invoice. */

const STATE_LABELS: Record<InvoiceState, string> = {
  paid: "Paid",
  failed: "Payment failed",
  void: "Void",
  pending: "Processing",
};

function dateLabel(createdAt: string | null): string {
  return createdAt ? createdAt.slice(0, 10) : "—";
}

function linesSummary(lines: InvoiceLine[]): string {
  if (lines.length === 0) return "—";
  return lines.map((line) => line.description).join(", ");
}

function receiptUrl(invoice: CustomInvoice): string | null {
  return invoice.hosted_invoice_url ?? invoice.invoice_pdf;
}

/** Whether the operator last left this section open. A private-mode throw just falls back to
 * the default instead of taking the page down. */
function readStoredOpen(storageKey: string, fallback: boolean): boolean {
  try {
    const stored = window.localStorage.getItem(storageKey);
    return stored == null ? fallback : stored === "true";
  } catch {
    return fallback;
  }
}

function Collapsible({
  storageKey,
  title,
  defaultOpen,
  children,
}: {
  storageKey: string;
  title: string;
  defaultOpen: boolean;
  children: ReactNode;
}): JSX.Element {
  const [open, setOpen] = useState(() => readStoredOpen(storageKey, defaultOpen));

  function toggle() {
    setOpen((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(storageKey, next ? "true" : "false");
      } catch {
        // Private-mode storage: the section just will not remember.
      }
      return next;
    });
  }

  return (
    <section className="rounded-[var(--cx-r-md,14px)] border border-border">
      <button
        type="button"
        aria-label={title}
        aria-expanded={open}
        onClick={toggle}
        className="flex w-full items-center justify-between gap-4 px-3 py-2 text-left"
      >
        <span className="text-sm font-medium">{title}</span>
        <span className="text-xs text-muted-foreground">{open ? "Hide" : "Show"}</span>
      </button>
      <div className="border-t border-border px-3 py-2" hidden={!open}>
        {children}
      </div>
    </section>
  );
}

export function InvoicesCard(): JSX.Element {
  const { api } = useAuth();
  const invoicesQuery = useMyInvoices(api);
  const invoices = invoicesQuery.data ?? [];

  return (
    <Collapsible storageKey="settings.billing.invoices" title="Invoices" defaultOpen={false}>
      {invoicesQuery.isLoading ? <Spinner label="Loading invoices" /> : null}

      {invoicesQuery.isError ? (
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(invoicesQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void invoicesQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : null}

      {!invoicesQuery.isLoading && !invoicesQuery.isError && invoices.length === 0 ? (
        <EmptyState
          title="No invoices yet."
          description="Invoices we charge to your card will show up here."
        />
      ) : null}

      {!invoicesQuery.isError && invoices.length > 0 ? (
        <ul className="rounded-[var(--cx-r-md,14px)] border border-border">
          {invoices.map((invoice) => {
            const receipt = receiptUrl(invoice);
            return (
              <li
                key={invoice.id}
                className="flex items-start justify-between gap-4 border-t border-border px-3 py-2 first:border-t-0"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium">{invoice.number ?? "Draft"}</span>
                    <span className="text-xs text-muted-foreground">
                      {dateLabel(invoice.created_at)}
                    </span>
                    <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium">
                      {STATE_LABELS[invoice.state]}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">{linesSummary(invoice.lines)}</p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="text-[13px] font-medium">
                    {formatCredits(invoice.total_micros)}
                  </span>
                  {receipt ? (
                    <a
                      href={receipt}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[12px] font-medium underline"
                    >
                      View / receipt
                    </a>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </Collapsible>
  );
}
