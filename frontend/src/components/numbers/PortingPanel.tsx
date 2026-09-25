import { useState, type FormEvent } from "react";

import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Textarea,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import type { ApiClient } from "@/api/client";
import {
  useCreatePortIn,
  usePortabilityCheck,
  usePorts,
  useSetPortLock,
  type PortEvent,
} from "@/api/numberSafety";

/** Everything the port-in form collects, minus the two file inputs. */
interface PortInDraft {
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
}

const EMPTY_DRAFT: PortInDraft = {
  numbers: "",
  carrier: "telnyx",
  authorized_name: "",
  business_name: "",
  account_number: "",
  pin: "",
  billing_number: "",
  service_street: "",
  service_extended: "",
  service_city: "",
  service_state: "",
  service_zip: "",
};

const DOCUMENT_ACCEPT = ".pdf,.png,.jpg,.jpeg";

/** Splits a textarea value on newlines/commas, trimming each entry and dropping blanks. */
function parseNumbers(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function portStatusPill(status: string, focDate: string | null) {
  switch (status) {
    case "awaiting_review":
      return <Pill tone="info">In review</Pill>;
    case "submitted":
    case "in_process":
      return <Pill tone="info">In progress</Pill>;
    case "foc_confirmed":
      return <Pill tone="info">Scheduled{focDate ? ` ${focDate}` : ""}</Pill>;
    case "ported":
      return <Pill tone="success">Complete</Pill>;
    case "exception":
      return <Pill tone="warning">Needs attention</Pill>;
    case "rejected":
      return <Pill tone="danger">Rejected</Pill>;
    case "cancelled":
      return <Pill tone="neutral">Cancelled</Pill>;
    default:
      return <Pill tone="neutral">{status}</Pill>;
  }
}

function eventText(event: PortEvent): string {
  const parts: string[] = [];
  if (event.at) parts.push(String(event.at));
  if (event.status) parts.push(String(event.status));
  if (event.note) parts.push(String(event.note));
  return parts.join(" — ");
}

export function PortingPanel({
  api,
  numbers,
}: {
  api: ApiClient;
  numbers: Array<{ id: string; e164: string; port_locked?: boolean | null }>;
}) {
  const [checkInput, setCheckInput] = useState("");
  const portabilityCheck = usePortabilityCheck(api);

  const [showPortForm, setShowPortForm] = useState(false);
  const [draft, setDraft] = useState<PortInDraft>(EMPTY_DRAFT);
  const [loa, setLoa] = useState<File | null>(null);
  const [invoice, setInvoice] = useState<File | null>(null);
  const [sentForReview, setSentForReview] = useState(false);
  const [showLocks, setShowLocks] = useState(false);
  const createPortIn = useCreatePortIn(api);

  const portsQuery = usePorts(api);
  const ports = portsQuery.data?.ports ?? [];

  const setPortLock = useSetPortLock(api);

  function handleCheck() {
    const list = parseNumbers(checkInput);
    if (list.length === 0) return;
    portabilityCheck.mutate(list);
  }

  function handlePortInSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!loa || !invoice) return;
    createPortIn.mutate(
      { ...draft, loa, invoice },
      {
        onSuccess: () => {
          setDraft(EMPTY_DRAFT);
          setLoa(null);
          setInvoice(null);
          setShowPortForm(false);
          setSentForReview(true);
        },
      },
    );
  }

  const checkList = parseNumbers(checkInput);
  const readyToSubmit = Boolean(loa && invoice);

  return (
    <Section
      title="Porting"
      description="Bring your existing numbers to us, and protect your numbers from being moved away."
    >
      <div className="space-y-6">
        {/* 1. Portability check */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Check if numbers can move</h3>
          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">
              Numbers to check, one per line or comma separated
            </span>
            <Textarea
              value={checkInput}
              onChange={(event) => setCheckInput(event.target.value)}
              rows={3}
              placeholder={"+14155550100\n+14155550101"}
            />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={handleCheck} disabled={portabilityCheck.isPending || checkList.length === 0}>
              Check
            </Button>
            <MutationStatus pending={portabilityCheck.isPending} error={portabilityCheck.error} />
          </div>
          {portabilityCheck.data && (
            <ul className="space-y-1">
              {portabilityCheck.data.results.map((result, index) => (
                <li
                  key={`${result.phone_number}-${index}`}
                  className="flex flex-wrap items-center gap-2 text-sm"
                >
                  <span className="font-medium">{formatPhone(result.phone_number)}</span>
                  <Pill tone={result.portable ? "success" : "danger"}>
                    {result.portable ? "Can be ported" : "Can't be ported"}
                  </Pill>
                  {result.reason && <span className="text-slate-500">{result.reason}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 2. Port-in request */}
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium text-slate-900">Port numbers in</h3>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setShowPortForm((open) => !open);
                setSentForReview(false);
              }}
            >
              Start a port request
            </Button>
          </div>

          <p className="text-sm text-slate-500">
            Names must match your verified business or a verified owner. Our team reviews every
            request before it is sent to the carrier.
          </p>

          {sentForReview && <p className="text-sm text-green-600">Request sent for review.</p>}

          {showPortForm && (
            <form className="space-y-3" onSubmit={handlePortInSubmit}>
              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Numbers to port, one per line or comma separated
                </span>
                <Textarea
                  value={draft.numbers}
                  onChange={(event) => setDraft({ ...draft, numbers: event.target.value })}
                  rows={3}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Carrier</span>
                <Select
                  value={draft.carrier}
                  onChange={(event) =>
                    setDraft({ ...draft, carrier: event.target.value as PortInDraft["carrier"] })
                  }
                >
                  <option value="telnyx">Standard</option>
                  <option value="signalwire">Alternate network</option>
                </Select>
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Authorized person on the old account
                </span>
                <Input
                  value={draft.authorized_name}
                  onChange={(event) => setDraft({ ...draft, authorized_name: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Business name on the old account
                </span>
                <Input
                  value={draft.business_name}
                  onChange={(event) => setDraft({ ...draft, business_name: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Account number with the old carrier
                </span>
                <Input
                  value={draft.account_number}
                  onChange={(event) => setDraft({ ...draft, account_number: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Account PIN / passcode</span>
                <Input
                  type="password"
                  value={draft.pin}
                  onChange={(event) => setDraft({ ...draft, pin: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Main billing phone number</span>
                <Input
                  type="tel"
                  value={draft.billing_number}
                  onChange={(event) => setDraft({ ...draft, billing_number: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Street address</span>
                <Input
                  value={draft.service_street}
                  onChange={(event) => setDraft({ ...draft, service_street: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Suite, optional</span>
                <Input
                  value={draft.service_extended}
                  onChange={(event) => setDraft({ ...draft, service_extended: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">City</span>
                <Input
                  value={draft.service_city}
                  onChange={(event) => setDraft({ ...draft, service_city: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">State (2 letters)</span>
                <Input
                  maxLength={2}
                  value={draft.service_state}
                  onChange={(event) =>
                    setDraft({ ...draft, service_state: event.target.value.toUpperCase() })
                  }
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">ZIP code</span>
                <Input
                  value={draft.service_zip}
                  onChange={(event) => setDraft({ ...draft, service_zip: event.target.value })}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Signed letter of authorization (PDF, PNG or JPEG)
                </span>
                <input
                  type="file"
                  accept={DOCUMENT_ACCEPT}
                  className="block w-full text-sm"
                  onChange={(event) => setLoa(event.target.files?.[0] ?? null)}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">
                  Recent bill from the old carrier
                </span>
                <input
                  type="file"
                  accept={DOCUMENT_ACCEPT}
                  className="block w-full text-sm"
                  onChange={(event) => setInvoice(event.target.files?.[0] ?? null)}
                />
              </label>

              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" disabled={createPortIn.isPending || !readyToSubmit}>
                  Submit port request
                </Button>
                <MutationStatus pending={createPortIn.isPending} error={createPortIn.error} />
              </div>
            </form>
          )}
        </div>

        {/* 3. Existing requests */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Your port requests</h3>
          {ports.length === 0 ? (
            <EmptyState title="No port requests" />
          ) : (
            <ul className="divide-y divide-slate-200">
              {ports.map((port) => (
                <li key={port.id} className="space-y-1 py-3">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">
                      {port.direction === "in" ? "Port in" : "Port out"}
                    </span>
                    <span className="text-slate-600">
                      {port.numbers.map((number) => formatPhone(number)).join(", ")}
                    </span>
                    {portStatusPill(port.status, port.foc_date)}
                  </div>
                  {port.last_error && (
                    <p className="text-sm text-red-600">{port.last_error}</p>
                  )}
                  {port.events.length > 0 && (
                    <details className="text-sm">
                      <summary className="cursor-pointer text-slate-600">History</summary>
                      <ul className="mt-1 space-y-1">
                        {port.events.map((event, index) => (
                          <li key={index} className="text-slate-500">
                            {eventText(event)}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 4. Port lock */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Port lock</h3>
          <p className="text-sm text-slate-500">
            A locked number can't be released or moved inside your account. To stop another carrier
            taking a number, keep your port-out PIN private.
          </p>
          {numbers.length === 0 ? (
            <p className="text-sm text-slate-500">You don't have any numbers yet.</p>
          ) : !showLocks ? (
            <Button variant="outline" size="sm" onClick={() => setShowLocks(true)}>
              Manage port locks ({numbers.filter((n) => n.port_locked).length} of {numbers.length} locked)
            </Button>
          ) : (
            <ul className="divide-y divide-slate-200">
              {numbers.map((number) => {
                const locked = Boolean(number.port_locked);
                return (
                  <li key={number.id} className="flex items-center justify-between gap-3 py-2">
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium">{formatPhone(number.e164)}</span>
                      {locked && <Pill tone="success">Locked</Pill>}
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={setPortLock.isPending}
                      onClick={() => setPortLock.mutate({ numberId: number.id, locked: !locked })}
                    >
                      {locked ? "Unlock" : "Lock"}
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
          <MutationStatus pending={setPortLock.isPending} error={setPortLock.error} />
        </div>
      </div>
    </Section>
  );
}
