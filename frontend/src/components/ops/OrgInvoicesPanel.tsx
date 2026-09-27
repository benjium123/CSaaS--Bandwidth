import * as React from "react";

import { formatCredits } from "@/api/billing";
import {
  INVOICE_PACKAGE_LABELS,
  INVOICE_PACKAGES,
  formatCents,
  useOrgInvoices,
  usePreviewInvoice,
  useRetryInvoice,
  useSendInvoice,
  useVoidInvoice,
  type InvoiceIn,
  type InvoiceLineInput,
  type InvoicePackage,
  type InvoicePreview,
  type InvoiceState,
} from "@/api/customInvoices";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";

/** Custom invoices for one workspace. The operator builds the invoice from lines (packages,
 * balance credit, custom items and a discount), the server prices it, and only then can the
 * card on file be charged. The server owns every price, so the preview is a request and the
 * charge is only ever enabled against a preview of the form as it stands right now - any edit
 * throws the preview away. */

const STATE_LABELS: Record<InvoiceState, string> = {
  pending: "Pending",
  paid: "Paid",
  failed: "Failed",
  void: "Void",
};

const STATE_BADGES: Record<InvoiceState, string> = {
  paid: "border-emerald-200 bg-emerald-50 text-emerald-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  void: "border-border bg-muted text-muted-foreground",
  pending: "border-border bg-muted",
};

const ACTION_BUTTON =
  "rounded-[var(--cx-r-md,14px)] border border-border bg-muted px-3 py-1 text-[13px] font-medium disabled:opacity-50";
const SMALL_BUTTON =
  "rounded-[var(--cx-r-md,14px)] border border-border px-2 py-1 text-[11px] font-medium disabled:opacity-50";
const FIELD =
  "w-full rounded-[var(--cx-r-md,14px)] border border-border bg-background px-2 py-1";

type DraftLineType = "package" | "credit" | "item";

type DiscountMode = "none" | "percent" | "amount";

interface DraftLine {
  key: string;
  type: DraftLineType;
  package: InvoicePackage;
  quantity: string;
  amount: string;
  description: string;
}

/** Monotonic so a row keeps its identity while another row is added or removed. */
let lineSeq = 0;

function blankLine(): DraftLine {
  lineSeq += 1;
  return {
    key: `draft-line-${lineSeq}`,
    type: "package",
    package: "sms",
    quantity: "1",
    amount: "",
    description: "",
  };
}

/** Dollars typed into an input -> integer cents. null when the field is empty or not a number. */
function centsFromDollars(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const dollars = Number(trimmed);
  if (!Number.isFinite(dollars)) return null;
  return Math.round(dollars * 100);
}

function lineProblem(line: DraftLine): string | null {
  if (line.type === "package") {
    const quantity = Number(line.quantity.trim());
    if (line.quantity.trim() === "" || !Number.isInteger(quantity) || quantity <= 0) {
      return "Bundles must be a whole number greater than zero.";
    }
    if (line.amount.trim() !== "") {
      const cents = centsFromDollars(line.amount);
      if (cents == null || cents < 0) return "The price must be a dollar amount.";
    }
    return null;
  }
  if (line.type === "credit") {
    const cents = centsFromDollars(line.amount);
    if (cents == null || cents <= 0) return "Enter the credit amount in dollars.";
    return null;
  }
  if (line.description.trim() === "") return "Describe the custom item.";
  const cents = centsFromDollars(line.amount);
  if (cents == null) return "Enter the item amount in dollars.";
  return null;
}

function discountProblem(mode: DiscountMode, value: string): string | null {
  if (mode === "none") return null;
  if (mode === "percent") {
    const percent = Number(value.trim());
    if (value.trim() === "" || !Number.isFinite(percent) || percent <= 0 || percent > 100) {
      return "Enter a discount between 0.01% and 100%.";
    }
    return null;
  }
  const cents = centsFromDollars(value);
  if (cents == null || cents <= 0) return "Enter the discount amount in dollars.";
  return null;
}

export function OrgInvoicesPanel({
  orgId,
  canEdit,
}: {
  orgId: string;
  canEdit: boolean;
}): JSX.Element {
  const { api } = useAuth();

  const invoicesQuery = useOrgInvoices(api, orgId);
  const previewMutation = usePreviewInvoice(api, orgId);
  const sendMutation = useSendInvoice(api, orgId);
  const retryMutation = useRetryInvoice(api, orgId);
  const voidMutation = useVoidInvoice(api, orgId);

  const invoices = invoicesQuery.data ?? [];

  const [formOpen, setFormOpen] = React.useState(false);
  const [lines, setLines] = React.useState<DraftLine[]>(() => [blankLine()]);
  const [discountMode, setDiscountMode] = React.useState<DiscountMode>("none");
  const [discountValue, setDiscountValue] = React.useState("");
  const [memo, setMemo] = React.useState("");
  const [preview, setPreview] = React.useState<InvoicePreview | null>(null);
  const [outcome, setOutcome] = React.useState<{ state: InvoiceState; error: string | null } | null>(
    null,
  );

  const problem =
    lines.map(lineProblem).find((message) => message !== null) ??
    discountProblem(discountMode, discountValue);
  const ready = lines.length > 0 && problem == null;

  function editLine(key: string, patch: Partial<DraftLine>) {
    setLines((current) =>
      current.map((line) => (line.key === key ? { ...line, ...patch } : line)),
    );
    // Any edit invalidates the price the server quoted for this form.
    setPreview(null);
  }

  function addLine() {
    setLines((current) => [...current, blankLine()]);
    setPreview(null);
  }

  function removeLine(key: string) {
    setLines((current) =>
      current.length > 1 ? current.filter((line) => line.key !== key) : current,
    );
    setPreview(null);
  }

  function buildBody(): InvoiceIn {
    const built: InvoiceLineInput[] = [];
    for (const line of lines) {
      if (line.type === "package") {
        const quantity = Number(line.quantity.trim());
        const cents = centsFromDollars(line.amount);
        built.push(
          cents == null
            ? { type: "package", package: line.package, quantity }
            : { type: "package", package: line.package, quantity, amount_cents: cents },
        );
      } else if (line.type === "credit") {
        built.push({ type: "credit", amount_cents: centsFromDollars(line.amount) ?? 0 });
      } else {
        built.push({
          type: "item",
          description: line.description.trim(),
          amount_cents: centsFromDollars(line.amount) ?? 0,
        });
      }
    }
    if (discountMode === "percent") {
      built.push({ type: "discount", percent: Number(discountValue.trim()) });
    } else if (discountMode === "amount") {
      built.push({ type: "discount", amount_cents: centsFromDollars(discountValue) ?? 0 });
    }
    const body: InvoiceIn = { lines: built };
    if (memo.trim() !== "") body.memo = memo.trim();
    return body;
  }

  function runPreview(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready) return;
    previewMutation.mutate(buildBody(), { onSuccess: (result) => setPreview(result) });
  }

  function clearForm() {
    setLines([blankLine()]);
    setDiscountMode("none");
    setDiscountValue("");
    setMemo("");
    setPreview(null);
  }

  function closeForm() {
    clearForm();
    setFormOpen(false);
  }

  function charge() {
    if (!ready || preview == null) return;
    sendMutation.mutate(buildBody(), {
      onSuccess: (invoice) => {
        setOutcome({ state: invoice.state, error: invoice.error });
        closeForm();
      },
    });
  }

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Invoices</SectionLabel>

      {invoicesQuery.isPending ? (
        <Spinner label="Loading invoices" />
      ) : invoicesQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(invoicesQuery.error)}
        </p>
      ) : invoices.length > 0 ? (
        <ul className="rounded-[var(--cx-r-md,14px)] border border-border">
          {invoices.map((invoice) => (
            <li
              key={invoice.id}
              className="flex items-start justify-between gap-4 border-t border-border px-3 py-2 first:border-t-0"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-medium">{invoice.number ?? "Draft"}</span>
                  <span
                    className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${STATE_BADGES[invoice.state]}`}
                  >
                    {STATE_LABELS[invoice.state]}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {formatCredits(invoice.total_micros)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {invoice.created_at ? invoice.created_at.slice(0, 10) : "—"}
                  </span>
                </div>
                {invoice.state === "failed" && invoice.error ? (
                  <p className="text-xs text-destructive">{invoice.error}</p>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {invoice.state === "paid" && invoice.hosted_invoice_url ? (
                  <a
                    href={invoice.hosted_invoice_url}
                    target="_blank"
                    rel="noreferrer"
                    className={SMALL_BUTTON}
                  >
                    View
                  </a>
                ) : null}
                {canEdit && (invoice.state === "failed" || invoice.state === "pending") ? (
                  <>
                    <button
                      type="button"
                      className={SMALL_BUTTON}
                      disabled={retryMutation.isPending && retryMutation.variables === invoice.id}
                      onClick={() => retryMutation.mutate(invoice.id)}
                    >
                      Charge again
                    </button>
                    <button
                      type="button"
                      className={SMALL_BUTTON}
                      disabled={voidMutation.isPending && voidMutation.variables === invoice.id}
                      onClick={() => voidMutation.mutate(invoice.id)}
                    >
                      Void
                    </button>
                  </>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <ConsoleEmpty>No invoices yet.</ConsoleEmpty>
      )}

      {outcome ? (
        <p
          role="status"
          className={
            outcome.state === "failed"
              ? "text-sm text-destructive"
              : "text-sm text-muted-foreground"
          }
        >
          {outcome.state === "paid"
            ? "Invoice paid"
            : outcome.state === "failed"
              ? `Card declined: ${outcome.error ?? "the card was declined"}`
              : "Invoice created."}
        </p>
      ) : null}

      {canEdit ? (
        formOpen ? (
          <form className="space-y-3" onSubmit={runPreview}>
            <fieldset className="space-y-2">
              <legend className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Lines
              </legend>
              {lines.map((line, index) => (
                <div
                  key={line.key}
                  className="space-y-2 rounded-[var(--cx-r-md,14px)] border border-border px-3 py-2"
                >
                  <div className="grid gap-2 sm:grid-cols-2">
                    <label className="space-y-1 text-xs">
                      <span className="block font-medium">Type</span>
                      <select
                        aria-label={`Line ${index + 1} type`}
                        value={line.type}
                        onChange={(event) =>
                          editLine(line.key, { type: event.target.value as DraftLineType })
                        }
                        className={FIELD}
                      >
                        <option value="package">Package</option>
                        <option value="credit">Balance credit</option>
                        <option value="item">Custom item</option>
                      </select>
                    </label>

                    {line.type === "package" ? (
                      <label className="space-y-1 text-xs">
                        <span className="block font-medium">Package</span>
                        <select
                          aria-label={`Line ${index + 1} package`}
                          value={line.package}
                          onChange={(event) =>
                            editLine(line.key, {
                              package: event.target.value as InvoicePackage,
                            })
                          }
                          className={FIELD}
                        >
                          {INVOICE_PACKAGES.map((pkg) => (
                            <option key={pkg} value={pkg}>
                              {INVOICE_PACKAGE_LABELS[pkg]}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}

                    {line.type === "package" ? (
                      <label className="space-y-1 text-xs">
                        <span className="block font-medium">Bundles</span>
                        <input
                          aria-label={`Line ${index + 1} quantity`}
                          type="number"
                          min="1"
                          step="1"
                          value={line.quantity}
                          onChange={(event) =>
                            editLine(line.key, { quantity: event.target.value })
                          }
                          className={FIELD}
                        />
                      </label>
                    ) : null}

                    {line.type === "item" ? (
                      <label className="space-y-1 text-xs">
                        <span className="block font-medium">Description</span>
                        <input
                          aria-label={`Line ${index + 1} description`}
                          type="text"
                          value={line.description}
                          onChange={(event) =>
                            editLine(line.key, { description: event.target.value })
                          }
                          className={FIELD}
                        />
                      </label>
                    ) : null}

                    <label className="space-y-1 text-xs">
                      <span className="block font-medium">
                        {line.type === "package" ? "Price $ (optional)" : "Amount $"}
                      </span>
                      <input
                        aria-label={
                          line.type === "package"
                            ? `Line ${index + 1} price`
                            : `Line ${index + 1} amount`
                        }
                        type="number"
                        min="0"
                        step="0.01"
                        value={line.amount}
                        onChange={(event) => editLine(line.key, { amount: event.target.value })}
                        className={FIELD}
                      />
                    </label>
                  </div>

                  {lines.length > 1 ? (
                    <button
                      type="button"
                      className={SMALL_BUTTON}
                      onClick={() => removeLine(line.key)}
                    >
                      Remove line {index + 1}
                    </button>
                  ) : null}
                </div>
              ))}
              <button type="button" className={SMALL_BUTTON} onClick={addLine}>
                Add line
              </button>
            </fieldset>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-xs">
                <span className="block font-medium">Discount</span>
                <select
                  aria-label="Discount type"
                  value={discountMode}
                  onChange={(event) => {
                    setDiscountMode(event.target.value as DiscountMode);
                    setPreview(null);
                  }}
                  className={FIELD}
                >
                  <option value="none">None</option>
                  <option value="percent">Percent off</option>
                  <option value="amount">Dollar amount off</option>
                </select>
              </label>

              {discountMode !== "none" ? (
                <label className="space-y-1 text-xs">
                  <span className="block font-medium">
                    {discountMode === "percent" ? "Discount %" : "Discount $"}
                  </span>
                  <input
                    aria-label="Discount value"
                    type="number"
                    min="0"
                    step="0.01"
                    value={discountValue}
                    onChange={(event) => {
                      setDiscountValue(event.target.value);
                      setPreview(null);
                    }}
                    className={FIELD}
                  />
                </label>
              ) : null}
            </div>

            <label className="space-y-1 text-xs">
              <span className="block font-medium">Memo (optional)</span>
              <textarea
                aria-label="Memo"
                rows={2}
                value={memo}
                onChange={(event) => {
                  setMemo(event.target.value);
                  setPreview(null);
                }}
                className={FIELD}
              />
            </label>

            {problem ? <p className="text-xs text-muted-foreground">{problem}</p> : null}

            {preview ? (
              <div className="space-y-1 rounded-[var(--cx-r-md,14px)] border border-border px-3 py-2">
                <ul className="space-y-1">
                  {preview.lines.map((line, index) => (
                    <li
                      key={`${line.type}-${index}`}
                      className="flex items-center justify-between gap-4 text-[13px]"
                    >
                      <span>{line.description}</span>
                      <span>{formatCents(line.amount_cents)}</span>
                    </li>
                  ))}
                </ul>
                <div className="flex items-center justify-between gap-4 text-[13px]">
                  <span className="text-muted-foreground">Subtotal</span>
                  <span>{formatCredits(preview.list)}</span>
                </div>
                <div className="flex items-center justify-between gap-4 text-[13px]">
                  <span className="text-muted-foreground">Discount</span>
                  <span>-{formatCredits(preview.discount)}</span>
                </div>
                <div className="flex items-center justify-between gap-4 text-[13px] font-medium">
                  <span>Total</span>
                  <span>{formatCredits(preview.paid)}</span>
                </div>
              </div>
            ) : null}

            <div className="flex flex-wrap gap-2">
              <button type="button" className={SMALL_BUTTON} onClick={closeForm}>
                Cancel
              </button>
              <button
                type="submit"
                disabled={!ready || previewMutation.isPending}
                className={ACTION_BUTTON}
              >
                Preview
              </button>
              <button
                type="button"
                disabled={preview == null || !ready || sendMutation.isPending}
                className={ACTION_BUTTON}
                onClick={charge}
              >
                Charge card
              </button>
            </div>

            <p className="text-xs text-muted-foreground">
              Charged to the workspace&apos;s card on file. Packages and credit are added once the
              payment succeeds.
            </p>

            {previewMutation.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {mutationErrorMessage(previewMutation.error)}
              </p>
            ) : null}

            {sendMutation.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {mutationErrorMessage(sendMutation.error)}
              </p>
            ) : null}
          </form>
        ) : (
          <button type="button" className={ACTION_BUTTON} onClick={() => setFormOpen(true)}>
            New invoice
          </button>
        )
      ) : null}

      {retryMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(retryMutation.error)}
        </p>
      ) : null}

      {voidMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(voidMutation.error)}
        </p>
      ) : null}
    </SurfaceCard>
  );
}
