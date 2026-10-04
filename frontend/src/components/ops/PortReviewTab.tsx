import { useState } from "react";

import type { ApiClient } from "@/api/client";
import {
  fetchPortDocument,
  useApprovePort,
  useAuthorizePortOut,
  useDecideGrant,
  useOpsPorts,
  usePendingGrants,
  usePortOutCodes,
  useRejectPort,
  useRejectPortOut,
  useSetPortStatus,
  type PendingGrant,
  type PortManualStatus,
  type PortRequest,
} from "@/api/numberSafety";
import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

/* ------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* ------------------------------------------------------------------------- */

/** Short, human-scannable form of a UUID/org id. */
function shortId(id: string): string {
  return id.slice(0, 8);
}

function orDash(value: string | null | undefined): string {
  return value && value.length > 0 ? value : "—";
}

/** Credit grants are in micro-dollars; bundle grants are a unit count plus a kind. */
function grantAmount(grant: PendingGrant): string {
  if (grant.type === "credit") {
    return `$${((grant.amount_micros ?? 0) / 1e6).toFixed(2)}`;
  }
  return `${grant.units ?? 0} ${grant.kind ?? ""}`.trim();
}

/** How long ops have to answer a port-out before it lapses. Six hours or less is treated as
 * urgent; so is anything already past. */
const PORT_OUT_URGENT_MS = 6 * 60 * 60 * 1000;

/* ------------------------------------------------------------------------- */
/* Grants waiting for a second operator                                       */
/* ------------------------------------------------------------------------- */

function GrantRow({ api, grant }: { api: ApiClient; grant: PendingGrant }) {
  const decide = useDecideGrant(api);

  return (
    <li className="rounded-md border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm">{shortId(grant.org_id)}</span>
        <Pill tone="neutral">{grant.type}</Pill>
        <span className="text-sm font-medium">{grantAmount(grant)}</span>
      </div>

      {grant.note ? <p className="mt-1 text-sm text-slate-600">{grant.note}</p> : null}

      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">Requested</dt>
        <dd>{grant.requested_at ? new Date(grant.requested_at).toLocaleString() : "—"}</dd>
        <dt className="text-slate-500">Requested by</dt>
        <dd className="font-mono">{shortId(grant.requested_by)}</dd>
      </dl>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ id: grant.id, approve: true })}
        >
          Approve
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ id: grant.id, approve: false })}
        >
          Reject
        </Button>
        <MutationStatus pending={decide.isPending} error={decide.error} />
      </div>
    </li>
  );
}

function PendingGrantsSection({ api }: { api: ApiClient }) {
  const grants = usePendingGrants(api);

  return (
    <Section
      title="Grants waiting for a second operator"
      description="Credit over $50 a day and large bundles need a different admin to approve."
    >
      {grants.isLoading ? (
        <Spinner />
      ) : grants.isError ? (
        <p className="text-sm text-red-600">{mutationErrorMessage(grants.error)}</p>
      ) : !grants.data || grants.data.length === 0 ? (
        <EmptyState title="Nothing waiting" />
      ) : (
        <ul className="space-y-3">
          {grants.data.map((grant) => (
            <GrantRow key={grant.id} api={api} grant={grant} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/* ------------------------------------------------------------------------- */
/* Port requests                                                              */
/* ------------------------------------------------------------------------- */

const PORT_STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "awaiting_review", label: "Waiting for review" },
  { value: "pending", label: "Port-out: waiting for decision" },
  { value: "", label: "All" },
  { value: "submitted", label: "Submitted" },
  { value: "in_process", label: "In process" },
  { value: "exception", label: "Exception" },
  { value: "foc_confirmed", label: "FOC confirmed" },
  { value: "ported", label: "Ported" },
  { value: "rejected", label: "Rejected" },
];

const MANUAL_STATUS_OPTIONS: { value: PortManualStatus; label: string }[] = [
  { value: "in_process", label: "In process" },
  { value: "exception", label: "Exception" },
  { value: "foc_confirmed", label: "FOC confirmed" },
  { value: "ported", label: "Ported" },
  { value: "cancelled", label: "Cancelled" },
];

/** Manual status changes do not apply once a port has settled one way or another, and the
 * awaiting-review card has its own approve/reject actions instead. */
const MANUAL_STATUS_HIDDEN = ["ported", "rejected", "cancelled", "awaiting_review"];

/** The ops decisions for one pending port-out. Split out so its queries and mutations only run
 * for the cards that actually have a decision to make. */
function PortOutDecision({ api, port }: { api: ApiClient; port: PortRequest }) {
  const authorize = useAuthorizePortOut(api);
  const reject = useRejectPortOut(api);

  const [mode, setMode] = useState<"idle" | "approve" | "reject">("idle");
  const [approveNote, setApproveNote] = useState("");
  const [code, setCode] = useState("");
  const [reason, setReason] = useState("");

  // The reason codes are only worth fetching once someone has started to reject.
  const codes = usePortOutCodes(api, port.id, mode === "reject");

  const codeList = codes.data?.codes ?? [];
  const selectedCode = codeList.find((entry) => String(entry.code) === code) ?? null;
  const reasonRequired = selectedCode?.reason_required ?? false;
  const reasonValue = reason.trim();
  const canReject = selectedCode !== null && (!reasonRequired || reasonValue.length >= 5);

  const respondBy = port.respond_by ? new Date(port.respond_by) : null;
  const respondByValid = respondBy !== null && !Number.isNaN(respondBy.getTime());
  const respondByUrgent = respondByValid
    ? respondBy.getTime() - Date.now() < PORT_OUT_URGENT_MS
    : false;

  return (
    <div className="space-y-2">
      {port.gaining_carrier ? (
        <p className="text-sm text-slate-600">Requested by {port.gaining_carrier}</p>
      ) : null}

      {port.respond_by ? (
        <p className={`text-sm ${respondByUrgent ? "text-red-600" : "text-slate-600"}`}>
          Respond by {respondByValid ? respondBy.toLocaleString() : port.respond_by}
        </p>
      ) : null}

      {port.disputed ? <Pill tone="danger">DISPUTED BY OWNER</Pill> : null}

      {mode === "idle" ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={authorize.isPending} onClick={() => setMode("approve")}>
            Approve transfer
          </Button>
          <Button variant="destructive" size="sm" onClick={() => setMode("reject")}>
            Reject
          </Button>
        </div>
      ) : null}

      {mode === "approve" ? (
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <p className="text-sm text-slate-600">
            Release these numbers to the new provider? This cannot be undone.
          </p>
          <label className="block text-sm">
            <span className="mb-1 block text-slate-500">Note (optional)</span>
            <Input value={approveNote} onChange={(e) => setApproveNote(e.target.value)} />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={authorize.isPending}
              onClick={() =>
                authorize.mutate(
                  { id: port.id, note: approveNote.trim() || undefined },
                  { onSuccess: () => setMode("idle") },
                )
              }
            >
              Confirm
            </Button>
            <Button variant="outline" size="sm" onClick={() => setMode("idle")}>
              Cancel
            </Button>
          </div>
          <MutationStatus pending={authorize.isPending} error={authorize.error} />
        </div>
      ) : null}

      {mode === "reject" ? (
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <p className="text-sm text-slate-500">
            Only reject for a real reason (PIN or details don't match, or the owner says it isn't
            them). A valid transfer can't be blocked for business reasons.
          </p>

          {codes.isLoading ? (
            <Spinner />
          ) : codes.isError ? (
            <p className="text-sm text-red-600">{mutationErrorMessage(codes.error)}</p>
          ) : (
            <label className="block text-sm">
              <span className="mb-1 block text-slate-500">Reason code</span>
              <Select
                aria-label="Reason code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
              >
                <option value="">Choose a reason</option>
                {codeList.map((entry) => (
                  <option key={entry.code} value={entry.code}>
                    {entry.label} ({entry.code})
                  </option>
                ))}
              </Select>
            </label>
          )}

          <label className="block text-sm">
            <span className="mb-1 block text-slate-500">Reason for rejection</span>
            <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {reasonRequired ? (
            <p className="text-xs text-slate-500">
              This reason code needs a short explanation (at least 5 characters).
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={!canReject || reject.isPending}
              onClick={() =>
                reject.mutate(
                  { id: port.id, code: Number(code), reason: reasonValue || undefined },
                  { onSuccess: () => setMode("idle") },
                )
              }
            >
              Reject
            </Button>
            <Button variant="outline" size="sm" onClick={() => setMode("idle")}>
              Cancel
            </Button>
          </div>
          <MutationStatus pending={reject.isPending} error={reject.error} />
        </div>
      ) : null}
    </div>
  );
}

function PortCard({ api, port }: { api: ApiClient; port: PortRequest }) {
  const approve = useApprovePort(api);
  const reject = useRejectPort(api);
  const setStatus = useSetPortStatus(api);

  const [reason, setReason] = useState("");
  const [manualStatus, setManualStatus] = useState<PortManualStatus>("in_process");
  const [focDate, setFocDate] = useState("");
  const [note, setNote] = useState("");
  const [docError, setDocError] = useState<string | null>(null);

  const showManualStatus = port.manual && !MANUAL_STATUS_HIDDEN.includes(port.status);
  const showPortOutDecision = port.direction === "out" && port.status === "pending";

  async function openDocument(kind: "loa" | "invoice") {
    setDocError(null);
    try {
      const blob = await fetchPortDocument(api, port.id, kind);
      window.open(URL.createObjectURL(blob), "_blank", "noopener");
    } catch (err) {
      setDocError(mutationErrorMessage(err));
    }
  }

  return (
    <li className="rounded-md border border-slate-200 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm">{shortId(port.org_id)}</span>
        <Pill tone="info">{port.direction === "in" ? "Port in" : "Port out"}</Pill>
        <span className="text-sm">{port.carrier}</span>
        {port.manual ? <Pill tone="warning">Manual filing</Pill> : null}
        <Pill tone="neutral">{port.status}</Pill>
      </div>

      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">Numbers</dt>
        <dd>{port.numbers.map((n) => formatPhone(n)).join(", ")}</dd>

        <dt className="text-slate-500">Authorized name</dt>
        <dd>{orDash(port.authorized_name)}</dd>

        <dt className="text-slate-500">Business name</dt>
        <dd>{orDash(port.business_name)}</dd>

        <dt className="text-slate-500">FOC date</dt>
        <dd>{orDash(port.foc_date)}</dd>

        <dt className="text-slate-500">Last error</dt>
        <dd>{orDash(port.last_error)}</dd>

        <dt className="text-slate-500">Created</dt>
        <dd>{orDash(port.created_at)}</dd>
      </dl>

      {port.events.length > 0 ? (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-slate-600">
            History ({port.events.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {port.events.map((event, index) => (
              <li key={index} className="text-slate-600">
                {event.at ? new Date(event.at).toLocaleString() : ""}
                {event.status ? ` · ${event.status}` : ""}
                {event.note ? ` · ${event.note}` : ""}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => openDocument("loa")}>
          Open LOA
        </Button>
        <Button variant="outline" size="sm" onClick={() => openDocument("invoice")}>
          Open invoice
        </Button>
      </div>
      {docError ? <p className="mt-1 text-sm text-red-600">{docError}</p> : null}

      {showPortOutDecision ? (
        <div className="mt-3 border-t border-slate-200 pt-3">
          <PortOutDecision api={api} port={port} />
        </div>
      ) : null}

      {port.status === "awaiting_review" ? (
        <div className="mt-3 space-y-2 border-t border-slate-200 pt-3">
          <p className="text-sm text-slate-600">
            Check the LOA and bill show the same numbers and account holder before approving.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={approve.isPending}
              onClick={() => approve.mutate(port.id)}
            >
              Approve
            </Button>
            <MutationStatus pending={approve.isPending} error={approve.error} />
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-[16rem] flex-1 text-sm">
              <span className="mb-1 block text-slate-500">Reason for rejection</span>
              <Input
                value={reason}
                placeholder="e.g. account holder does not match"
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <Button
              variant="destructive"
              size="sm"
              disabled={reason.trim().length < 3 || reject.isPending}
              onClick={() => reject.mutate({ id: port.id, reason: reason.trim() })}
            >
              Reject
            </Button>
          </div>
          <MutationStatus pending={reject.isPending} error={reject.error} />
        </div>
      ) : null}

      {showManualStatus ? (
        <div className="mt-3 space-y-2 border-t border-slate-200 pt-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="text-sm">
              <span className="mb-1 block text-slate-500">New status</span>
              <Select
                value={manualStatus}
                onChange={(e) => setManualStatus(e.target.value as PortManualStatus)}
              >
                {MANUAL_STATUS_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </label>

            <label className="text-sm">
              <span className="mb-1 block text-slate-500">FOC date (optional)</span>
              <Input type="date" value={focDate} onChange={(e) => setFocDate(e.target.value)} />
            </label>

            <label className="text-sm">
              <span className="mb-1 block text-slate-500">Note (optional)</span>
              <Input value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={setStatus.isPending}
              onClick={() =>
                setStatus.mutate({
                  id: port.id,
                  status: manualStatus,
                  foc_date: focDate || null,
                  note,
                })
              }
            >
              Update status
            </Button>
            <MutationStatus pending={setStatus.isPending} error={setStatus.error} />
          </div>
        </div>
      ) : null}
    </li>
  );
}

function PortRequestsSection({ api }: { api: ApiClient }) {
  const [status, setStatus] = useState("awaiting_review");
  const ports = useOpsPorts(api, status || undefined);

  return (
    <Section title="Port requests">
      <label className="mb-4 block max-w-xs text-sm">
        <span className="mb-1 block text-slate-500">Status</span>
        <Select value={status} onChange={(e) => setStatus(e.target.value)}>
          {PORT_STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </label>

      {ports.isLoading ? (
        <Spinner />
      ) : ports.isError ? (
        <p className="text-sm text-red-600">{mutationErrorMessage(ports.error)}</p>
      ) : !ports.data || ports.data.ports.length === 0 ? (
        <EmptyState title="No port requests" />
      ) : (
        <ul className="space-y-3">
          {ports.data.ports.map((port) => (
            <PortCard key={port.id} api={api} port={port} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/* ------------------------------------------------------------------------- */
/* Tab                                                                        */
/* ------------------------------------------------------------------------- */

export function PortReviewTab() {
  const { api } = useAuth();

  return (
    <div className="space-y-6">
      <PendingGrantsSection api={api} />
      <PortRequestsSection api={api} />
    </div>
  );
}
