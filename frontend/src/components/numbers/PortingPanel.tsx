import { useRef, useState, type FormEvent } from "react";

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
  useCancelPort,
  useCreatePortIn,
  useDisputePortOut,
  usePortabilityCheck,
  usePortPin,
  usePorts,
  useRevealPortPin,
  useRotatePortPin,
  useSetPortLock,
  useUpdatePortIn,
  type PortEvent,
  type PortInUpdate,
  type PortRequest,
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

/** How many history lines a collapsed timeline shows. */
const EVENT_PREVIEW_LIMIT = 5;

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

/** The old event shape's line, kept as the fallback for events that carry no `text`. */
function eventText(event: PortEvent): string {
  const parts: string[] = [];
  if (event.at) parts.push(String(event.at));
  if (event.status) parts.push(String(event.status));
  if (event.note) parts.push(String(event.note));
  return parts.join(" — ");
}

function eventLine(event: PortEvent): string {
  return event.text ? String(event.text) : eventText(event);
}

/** "Tue, Sep 17" - short enough to sit inside a sentence, and the weekday is the part a
 * customer checking their calendar actually needs. Exported so the test can compute the
 * expected line with this exact formatter rather than hard-coding a locale's output. */
export function formatPortFocDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

/** The one line a customer reads to know where their transfer is, in their own words. Port-out
 * requests come in with a different, much shorter set of statuses. */
function portStepText(port: PortRequest): string {
  if (port.direction === "out") {
    switch (port.status) {
      case "pending":
        return "Another provider asked for these numbers";
      case "authorized":
        return "Approved — moving away";
      case "rejected":
        return "Not released";
      case "ported":
        return "Moved away";
      case "cancelled":
        return "Cancelled";
      default:
        return port.status;
    }
  }

  switch (port.status) {
    case "awaiting_review":
      return "Waiting for our review";
    case "submitted":
      return "Filed — waiting for your current provider";
    case "in_process":
      return "In progress with your current provider";
    case "foc_confirmed":
      return port.foc_date ? `Moving on ${formatPortFocDate(port.foc_date)}` : "Moving soon";
    case "exception":
      return "Needs your attention";
    case "ported":
      return "Done — your numbers are live";
    case "rejected":
      return "Not accepted";
    case "cancelled":
      return "Cancelled";
    default:
      return port.status;
  }
}

/** The edit form starts from what the API already has, never from a blank slate - the
 * customer is fixing one wrong field, not retyping the request. */
function editDraftFromPort(port: PortRequest): PortInUpdate {
  return {
    authorized_name: port.authorized_name ?? "",
    business_name: port.business_name ?? "",
    account_number: port.account_number ?? "",
    pin: "",
    billing_number: port.billing_number ?? "",
    service_street: port.service_address?.street ?? "",
    service_extended: port.service_address?.extended ?? "",
    service_city: port.service_address?.city ?? "",
    service_state: port.service_address?.state ?? "",
    service_zip: port.service_address?.zip ?? "",
  };
}

/** The amber "we are on it" note, same treatment as `customer_reason`. */
function amberNote(text: string) {
  return (
    <p className="rounded-[var(--cx-r-sm,12px)] bg-[hsl(var(--cx-flag)/0.15)] px-3 py-2 text-sm text-[hsl(var(--cx-flag))]">
      {text}
    </p>
  );
}

/** The workspace transfer PIN, for owners/admins: masked until asked for, rotatable behind an
 * inline confirmation. The revealed digits live only in this component's state, so unmounting
 * the card (or the panel) hides them again. */
function TransferPinCard({ api }: { api: ApiClient }) {
  const pinStatus = usePortPin(api, true);
  const revealPin = useRevealPortPin(api);
  const rotatePin = useRotatePortPin(api);

  const [revealedPin, setRevealedPin] = useState<string | null>(null);
  const [confirmingChange, setConfirmingChange] = useState(false);

  function handleReveal() {
    revealPin.mutate(undefined, {
      onSuccess: (data) => setRevealedPin(data.pin ?? null),
    });
  }

  function handleRotate() {
    rotatePin.mutate(undefined, {
      onSuccess: (data) => {
        setRevealedPin(data.pin ?? null);
        setConfirmingChange(false);
      },
    });
  }

  return (
    <div className="space-y-2 rounded-[var(--cx-r-sm,12px)] border border-border p-3">
      <h4 className="text-sm font-medium text-slate-900">Transfer PIN</h4>

      <div className="flex flex-wrap items-center gap-2">
        {revealedPin ? (
          <>
            <span className="font-mono text-lg tracking-widest">{revealedPin}</span>
            <Button variant="outline" size="sm" onClick={() => setRevealedPin(null)}>
              Hide
            </Button>
          </>
        ) : (
          <>
            <span className="font-mono text-lg tracking-widest">●●●●●●</span>
            <Button
              variant="outline"
              size="sm"
              disabled={revealPin.isPending}
              onClick={handleReveal}
            >
              Show PIN
            </Button>
          </>
        )}

        {!confirmingChange && (
          <Button variant="outline" size="sm" onClick={() => setConfirmingChange(true)}>
            Change PIN
          </Button>
        )}
      </div>

      {confirmingChange && (
        <div className="space-y-2 rounded-[var(--cx-r-sm,12px)] border border-border p-3">
          <p className="text-sm text-slate-600">
            Change the PIN? The old one stops working on all your numbers.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={rotatePin.isPending} onClick={handleRotate}>
              Confirm
            </Button>
            <Button variant="outline" size="sm" onClick={() => setConfirmingChange(false)}>
              Keep it
            </Button>
          </div>
        </div>
      )}

      <MutationStatus pending={revealPin.isPending} error={revealPin.error} />
      <MutationStatus pending={rotatePin.isPending} error={rotatePin.error} />

      {pinStatus.data && pinStatus.data.numbers_total > 0 ? (
        <p className="text-sm text-slate-500">
          Protects {pinStatus.data.numbers_protected} of {pinStatus.data.numbers_total} numbers
        </p>
      ) : null}
    </div>
  );
}

function PortRequestCard({ api, port }: { api: ApiClient; port: PortRequest }) {
  const updatePortIn = useUpdatePortIn(api);
  const cancelPort = useCancelPort(api);
  const disputePort = useDisputePortOut(api);

  const [showAllEvents, setShowAllEvents] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState<PortInUpdate>(() => editDraftFromPort(port));
  const [editLoa, setEditLoa] = useState<File | null>(null);
  const [editInvoice, setEditInvoice] = useState<File | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [confirmingDispute, setConfirmingDispute] = useState(false);

  const isPortOut = port.direction === "out";

  // The API lists history oldest-first; a customer wants the newest line at the top.
  const newestFirst = [...port.events].reverse();
  const visibleEvents = showAllEvents ? newestFirst : newestFirst.slice(0, EVENT_PREVIEW_LIMIT);

  function handleEditSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    updatePortIn.mutate(
      { id: port.id, form: { ...editDraft, loa: editLoa, invoice: editInvoice } },
      {
        onSuccess: () => {
          setEditing(false);
          setEditLoa(null);
          setEditInvoice(null);
        },
      },
    );
  }

  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{port.direction === "in" ? "Port in" : "Port out"}</span>
        <span className="text-slate-600">
          {port.numbers.map((number) => formatPhone(number)).join(", ")}
        </span>
        {portStatusPill(port.status, port.foc_date)}
      </div>

      <p className="text-sm text-slate-600">{portStepText(port)}</p>

      {isPortOut && port.gaining_carrier && (
        <p className="text-sm text-slate-600">Requested by {port.gaining_carrier}</p>
      )}

      {isPortOut && port.disputed && amberNote("You told us this wasn't you. Our team is on it.")}

      {!isPortOut && port.customer_reason && amberNote(port.customer_reason)}

      {port.last_error && <p className="text-sm text-red-600">{port.last_error}</p>}

      {visibleEvents.length > 0 && (
        <ul className="space-y-1 text-sm">
          {visibleEvents.map((event, index) => (
            <li key={index} className="text-slate-500">
              {eventLine(event)}
            </li>
          ))}
        </ul>
      )}

      {newestFirst.length > EVENT_PREVIEW_LIMIT && (
        <button
          type="button"
          className="text-sm text-slate-600 underline"
          onClick={() => setShowAllEvents((open) => !open)}
        >
          {showAllEvents ? "Show less" : "Show all"}
        </button>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {port.can_edit && !editing && (
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
            {port.status === "awaiting_review" ? "Edit details" : "Fix and resubmit"}
          </Button>
        )}
        {port.can_cancel && !confirmingCancel && (
          <Button variant="outline" size="sm" onClick={() => setConfirmingCancel(true)}>
            Cancel transfer
          </Button>
        )}
        {isPortOut && port.can_dispute && !port.disputed && !confirmingDispute && (
          <Button
            variant="outline"
            size="sm"
            className="border-red-500 text-red-600"
            onClick={() => setConfirmingDispute(true)}
          >
            I didn't request this
          </Button>
        )}
      </div>

      {confirmingDispute && (
        <div className="space-y-2 rounded-[var(--cx-r-sm,12px)] border border-border p-3">
          <p className="text-sm text-slate-600">
            Tell Ringlite this transfer is not yours? We'll stop it if we can.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={disputePort.isPending}
              onClick={() =>
                disputePort.mutate(port.id, { onSuccess: () => setConfirmingDispute(false) })
              }
            >
              Confirm
            </Button>
            <Button variant="outline" size="sm" onClick={() => setConfirmingDispute(false)}>
              Cancel
            </Button>
          </div>
          <MutationStatus pending={disputePort.isPending} error={disputePort.error} />
        </div>
      )}

      {confirmingCancel && (
        <div className="space-y-2 rounded-[var(--cx-r-sm,12px)] border border-border p-3">
          <p className="text-sm text-slate-600">
            Stop this transfer? Your numbers stay with your current provider.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={cancelPort.isPending}
              onClick={() =>
                cancelPort.mutate(port.id, { onSuccess: () => setConfirmingCancel(false) })
              }
            >
              Confirm
            </Button>
            <Button variant="outline" size="sm" onClick={() => setConfirmingCancel(false)}>
              Keep it
            </Button>
          </div>
          <MutationStatus pending={cancelPort.isPending} error={cancelPort.error} />
        </div>
      )}

      {editing && (
        <form className="space-y-3" onSubmit={handleEditSubmit}>
          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">
              Authorized person on the old account
            </span>
            <Input
              value={editDraft.authorized_name}
              onChange={(event) =>
                setEditDraft({ ...editDraft, authorized_name: event.target.value })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">
              Business name on the old account
            </span>
            <Input
              value={editDraft.business_name}
              onChange={(event) => setEditDraft({ ...editDraft, business_name: event.target.value })}
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">
              Account number with the old carrier
            </span>
            <Input
              value={editDraft.account_number}
              onChange={(event) =>
                setEditDraft({ ...editDraft, account_number: event.target.value })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Account PIN / passcode</span>
            <Input
              type="password"
              value={editDraft.pin ?? ""}
              placeholder="Leave empty to keep the current PIN"
              onChange={(event) => setEditDraft({ ...editDraft, pin: event.target.value })}
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Main billing phone number</span>
            <Input
              type="tel"
              value={editDraft.billing_number}
              onChange={(event) =>
                setEditDraft({ ...editDraft, billing_number: event.target.value })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Street address</span>
            <Input
              value={editDraft.service_street}
              onChange={(event) =>
                setEditDraft({ ...editDraft, service_street: event.target.value })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Suite, optional</span>
            <Input
              value={editDraft.service_extended}
              onChange={(event) =>
                setEditDraft({ ...editDraft, service_extended: event.target.value })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">City</span>
            <Input
              value={editDraft.service_city}
              onChange={(event) => setEditDraft({ ...editDraft, service_city: event.target.value })}
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">State (2 letters)</span>
            <Input
              maxLength={2}
              value={editDraft.service_state}
              onChange={(event) =>
                setEditDraft({ ...editDraft, service_state: event.target.value.toUpperCase() })
              }
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">ZIP code</span>
            <Input
              value={editDraft.service_zip}
              onChange={(event) => setEditDraft({ ...editDraft, service_zip: event.target.value })}
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Replace authorization letter</span>
            <input
              type="file"
              accept={DOCUMENT_ACCEPT}
              className="block w-full text-sm"
              onChange={(event) => setEditLoa(event.target.files?.[0] ?? null)}
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">Replace bill</span>
            <input
              type="file"
              accept={DOCUMENT_ACCEPT}
              className="block w-full text-sm"
              onChange={(event) => setEditInvoice(event.target.files?.[0] ?? null)}
            />
          </label>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={updatePortIn.isPending}>
              Save
            </Button>
            <Button type="button" variant="outline" onClick={() => setEditing(false)}>
              Discard changes
            </Button>
            <MutationStatus pending={updatePortIn.isPending} error={updatePortIn.error} />
          </div>
        </form>
      )}
    </li>
  );
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
  const checkBoxRef = useRef<HTMLTextAreaElement | null>(null);

  const [showPortForm, setShowPortForm] = useState(false);
  // True while the form's numbers came from a portability check: the list is the answer we
  // just verified, so it is shown read-only until the customer asks to change it.
  const [numbersLocked, setNumbersLocked] = useState(false);
  const [draft, setDraft] = useState<PortInDraft>(EMPTY_DRAFT);
  const [loa, setLoa] = useState<File | null>(null);
  const [invoice, setInvoice] = useState<File | null>(null);
  const [sentForReview, setSentForReview] = useState(false);
  const [showLocks, setShowLocks] = useState(false);
  const createPortIn = useCreatePortIn(api);

  const portsQuery = usePorts(api);
  // A cleared query cache (the auth provider clears it when it pins the workspace) makes the
  // query hand back no data at all for a beat. Rendering straight from that would unmount
  // every card - flashing "No port requests", and throwing away a half-filled edit form - so
  // the list falls back to the last one we saw. A list the API really did return empty still
  // wins, because an empty list is data, not the absence of it.
  const lastPorts = useRef<PortRequest[]>([]);
  if (portsQuery.data) lastPorts.current = portsQuery.data.ports;
  const ports = portsQuery.data?.ports ?? lastPorts.current;

  const setPortLock = useSetPortLock(api);

  // The account number is the API's to mint; the panel just shows it. Omitting the line when
  // it is not there yet beats inventing one from the org id.
  const accountId = portsQuery.data?.account_id ?? null;
  const pinHolder = Boolean(portsQuery.data?.pin_holder);

  const checkResults = portabilityCheck.data?.results ?? [];
  const portableNumbers = checkResults
    .filter((result) => result.portable)
    .map((result) => result.phone_number);

  function handleCheck() {
    const list = parseNumbers(checkInput);
    if (list.length === 0) return;
    portabilityCheck.mutate(list);
  }

  function handleStartFromCheck() {
    setSentForReview(false);
    setLoa(null);
    setInvoice(null);
    setDraft({ ...EMPTY_DRAFT, numbers: portableNumbers.join("\n") });
    setNumbersLocked(true);
    setShowPortForm(true);
  }

  function handleStartManually() {
    setSentForReview(false);
    if (showPortForm) {
      setShowPortForm(false);
      return;
    }
    setLoa(null);
    setInvoice(null);
    setDraft(EMPTY_DRAFT);
    setNumbersLocked(false);
    setShowPortForm(true);
  }

  function handleChangeNumbers() {
    setShowPortForm(false);
    setNumbersLocked(false);
    checkBoxRef.current?.focus();
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
          setNumbersLocked(false);
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
              ref={checkBoxRef}
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
            <div className="space-y-2">
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
              {portableNumbers.length > 0 && (
                <Button variant="outline" size="sm" onClick={handleStartFromCheck}>
                  Start a transfer for {portableNumbers.length} portable number(s)
                </Button>
              )}
            </div>
          )}
        </div>

        {/* 2. Port-in request */}
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium text-slate-900">Port numbers in</h3>
            <Button variant="outline" size="sm" onClick={handleStartManually}>
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
                  readOnly={numbersLocked}
                  onChange={(event) => setDraft({ ...draft, numbers: event.target.value })}
                  rows={3}
                />
              </label>

              {numbersLocked && (
                <button
                  type="button"
                  className="text-sm text-slate-600 underline"
                  onClick={handleChangeNumbers}
                >
                  Change numbers
                </button>
              )}

              <label className="block">
                <span className="mb-1 block text-sm text-slate-600">Network</span>
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
                <span className="mt-1 block text-xs text-slate-400">
                  Mobile carriers (AT&T, T-Mobile, Verizon) give you a separate Number Transfer
                  PIN — request it in their app or by phone. For other providers it is usually your
                  account PIN.
                </span>
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
                <PortRequestCard key={port.id} api={api} port={port} />
              ))}
            </ul>
          )}
        </div>

        {/* 4. Moving a number away (port out) */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Moving a number away</h3>
          <p className="text-sm text-slate-500">
            Ask your new provider to start a transfer and give them:{" "}
            <span className="font-medium text-slate-900">Account number:</span> the phone number
            you are moving. <span className="font-medium text-slate-900">Transfer PIN:</span> your
            workspace PIN (owners and admins can see it below).{" "}
            <span className="font-medium text-slate-900">Name and address:</span> the business name
            and service address on this workspace.
          </p>
          <p className="text-sm text-slate-500">
            We review every transfer request before your number is released. You'll get an email.
          </p>
          {accountId && (
            <p className="text-xs text-slate-400">
              Ringlite account ID: <span className="font-medium">{accountId}</span>
            </p>
          )}
          {pinHolder ? (
            <TransferPinCard api={api} />
          ) : (
            <p className="text-sm text-slate-500">
              Only the workspace owner or an admin can see the transfer PIN.
            </p>
          )}
        </div>

        {/* 5. Port lock */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Port lock</h3>
          <p className="text-sm text-slate-500">
            Port lock stops a number being released or deleted inside Ringlite, even by someone
            signed in to your account. It does not affect transfers to another provider — those are
            protected by your transfer PIN and our review.
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
