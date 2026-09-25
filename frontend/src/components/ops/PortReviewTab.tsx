import { useState } from "react";

import type { ApiClient } from "@/api/client";
import {
  fetchPortDocument,
  useApprovePort,
  useDecideGrant,
  useOpsPorts,
  usePendingGrants,
  useRejectPort,
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
