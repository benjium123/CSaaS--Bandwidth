import { useState, type FormEvent } from "react";

import type { ApiClient } from "@/api/client";
import {
  addressSuggestions,
  useAssignEmergencyAddress,
  useCreateEmergencyAddress,
  useEmergencyAddresses,
  type AddressIn,
  type AddressSuggestion,
  type E911Number,
  type EmergencyAddress,
} from "@/api/numberSafety";
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

interface FormState {
  caller_name: string;
  label: string;
  line1: string;
  line2: string;
  city: string;
  state: string;
  postal_code: string;
}

const EMPTY_FORM: FormState = {
  caller_name: "",
  label: "",
  line1: "",
  line2: "",
  city: "",
  state: "",
  postal_code: "",
};

const SECTION_TITLE = "Emergency (911) addresses";
const SECTION_DESCRIPTION =
  "Every number needs a 911 address before it can make calls. Emergency services are sent to this address.";

/** Carriers name the same field differently (line1 vs street_address vs street, ...), so
 * read the first key that actually carries a value. */
function pick(source: AddressSuggestion, keys: string[]): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return "";
}

/** The bits of a carrier suggestion worth showing to a human: street, city, state, ZIP. */
function suggestionParts(suggestion: AddressSuggestion): string[] {
  return [
    pick(suggestion, ["line1", "street_address", "street"]),
    pick(suggestion, ["city", "locality"]),
    pick(suggestion, ["state", "administrative_area"]),
    pick(suggestion, ["postal_code", "zip"]),
  ].filter((part) => part !== "");
}

function oneLineAddress(address: EmergencyAddress): string {
  const cityState = [address.city, [address.state, address.postal_code].filter(Boolean).join(" ")]
    .filter(Boolean)
    .join(", ");
  return [address.line1, address.line2, cityState].filter(Boolean).join(", ");
}

function NumberStatus({ number }: { number: E911Number }) {
  if (number.e911_status === "active") return <Pill tone="success">Active</Pill>;
  if (number.e911_status === "pending") return <Pill tone="info">Being set up</Pill>;
  if (number.e911_status === "failed") {
    return (
      <div className="space-y-1">
        <Pill tone="danger">Failed</Pill>
        {number.e911_error ? <p className="text-xs text-red-600">{number.e911_error}</p> : null}
      </div>
    );
  }
  return <Pill tone="warning">Address needed</Pill>;
}

function AddressStatus({ address }: { address: EmergencyAddress }) {
  if (address.status === "valid") return <Pill tone="success">Valid</Pill>;
  if (address.status === "pending") return <Pill tone="info">Being checked</Pill>;
  if (address.status === "invalid") {
    return (
      <div className="space-y-1">
        <Pill tone="danger">Invalid</Pill>
        {address.last_error ? <p className="text-xs text-red-600">{address.last_error}</p> : null}
      </div>
    );
  }
  return <Pill tone="neutral">{address.status}</Pill>;
}

export function EmergencyAddressPanel({ api }: { api: ApiClient }) {
  const query = useEmergencyAddresses(api);
  const assign = useAssignEmergencyAddress(api);
  const create = useCreateEmergencyAddress(api);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const numbers = query.data?.numbers ?? [];
  const addresses = query.data?.addresses ?? [];
  const suggestions = addressSuggestions(create.error);

  function setField(key: keyof FormState, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function applySuggestion(suggestion: AddressSuggestion) {
    setForm((prev) => ({
      ...prev,
      line1: pick(suggestion, ["line1", "street_address", "street"]) || prev.line1,
      city: pick(suggestion, ["city", "locality"]) || prev.city,
      state: pick(suggestion, ["state", "administrative_area"]) || prev.state,
      postal_code: pick(suggestion, ["postal_code", "zip"]) || prev.postal_code,
    }));
  }

  function toggleForm() {
    create.reset();
    setShowForm((open) => !open);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const body: AddressIn = {
      caller_name: form.caller_name,
      label: form.label || undefined,
      line1: form.line1,
      line2: form.line2 || undefined,
      city: form.city,
      state: form.state.toUpperCase(),
      postal_code: form.postal_code,
      country: "US",
    };
    create.mutate(body, {
      onSuccess: () => {
        setForm(EMPTY_FORM);
        setShowForm(false);
      },
    });
  }

  if (query.isLoading) return <Spinner />;

  if (query.isError) {
    return (
      <Section title={SECTION_TITLE} description={SECTION_DESCRIPTION}>
        <p className="text-sm text-red-600">
          {mutationErrorMessage(query.error)}
        </p>
      </Section>
    );
  }

  return (
    <Section title={SECTION_TITLE} description={SECTION_DESCRIPTION}>
      <div className="space-y-6">
        <div className="space-y-2">
          {numbers.length === 0 ? (
            <EmptyState title="No numbers yet" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2 pr-4 font-medium">Number</th>
                    <th className="py-2 pr-4 font-medium">911 status</th>
                    <th className="py-2 font-medium">911 address</th>
                  </tr>
                </thead>
                <tbody>
                  {numbers.map((number) => (
                    <tr key={number.id} className="border-b border-slate-100 last:border-0">
                      <td className="py-3 pr-4 font-medium text-slate-900">
                        {formatPhone(number.e164)}
                      </td>
                      <td className="py-3 pr-4">
                        <NumberStatus number={number} />
                      </td>
                      <td className="py-3">
                        <Select
                          aria-label={`911 address for ${formatPhone(number.e164)}`}
                          value={number.emergency_address_id ?? ""}
                          onChange={(event) => {
                            const addressId = event.target.value;
                            if (!addressId) return;
                            assign.mutate({ numberId: number.id, addressId });
                          }}
                        >
                          <option value="">Choose address…</option>
                          {addresses.map((address) => (
                            <option key={address.id} value={address.id}>
                              {address.label || `${address.line1}, ${address.city}`}
                            </option>
                          ))}
                        </Select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <MutationStatus
            pending={assign.isPending}
            error={assign.error}
            pendingLabel="Saving 911 address…"
          />
        </div>

        <div className="space-y-2">
          <h3 className="text-sm font-medium text-slate-900">Addresses</h3>
          {addresses.length === 0 ? (
            <p className="text-sm text-slate-500">No addresses yet.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {addresses.map((address) => (
                <li key={address.id} className="flex items-start justify-between gap-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-900">
                      {address.label || "Address"}
                    </p>
                    <p className="text-sm text-slate-600">{oneLineAddress(address)}</p>
                  </div>
                  <AddressStatus address={address} />
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="space-y-3">
          <Button variant="outline" size="sm" onClick={toggleForm}>
            {showForm ? "Cancel" : "Add address"}
          </Button>

          {showForm ? (
            <form className="space-y-4" onSubmit={handleSubmit}>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label
                    htmlFor="ea-caller-name"
                    className="block text-sm font-medium text-slate-700"
                  >
                    Name shown to 911
                  </label>
                  <Input
                    id="ea-caller-name"
                    value={form.caller_name}
                    onChange={(event) => setField("caller_name", event.target.value)}
                    required
                  />
                </div>
                <div>
                  <label htmlFor="ea-label" className="block text-sm font-medium text-slate-700">
                    Label, optional
                  </label>
                  <Input
                    id="ea-label"
                    value={form.label}
                    onChange={(event) => setField("label", event.target.value)}
                  />
                </div>
                <div className="sm:col-span-2">
                  <label htmlFor="ea-line1" className="block text-sm font-medium text-slate-700">
                    Street address
                  </label>
                  <Input
                    id="ea-line1"
                    value={form.line1}
                    onChange={(event) => setField("line1", event.target.value)}
                    required
                  />
                </div>
                <div className="sm:col-span-2">
                  <label htmlFor="ea-line2" className="block text-sm font-medium text-slate-700">
                    Apt / suite, optional
                  </label>
                  <Input
                    id="ea-line2"
                    value={form.line2}
                    onChange={(event) => setField("line2", event.target.value)}
                  />
                </div>
                <div>
                  <label htmlFor="ea-city" className="block text-sm font-medium text-slate-700">
                    City
                  </label>
                  <Input
                    id="ea-city"
                    value={form.city}
                    onChange={(event) => setField("city", event.target.value)}
                    required
                  />
                </div>
                <div>
                  <label htmlFor="ea-state" className="block text-sm font-medium text-slate-700">
                    State
                  </label>
                  <Input
                    id="ea-state"
                    value={form.state}
                    onChange={(event) => setField("state", event.target.value.toUpperCase())}
                    maxLength={2}
                    placeholder="2-letter"
                    required
                  />
                </div>
                <div>
                  <label
                    htmlFor="ea-postal-code"
                    className="block text-sm font-medium text-slate-700"
                  >
                    ZIP code
                  </label>
                  <Input
                    id="ea-postal-code"
                    value={form.postal_code}
                    onChange={(event) => setField("postal_code", event.target.value)}
                    required
                  />
                </div>
              </div>

              <p className="text-xs text-slate-500">US addresses only for now.</p>

              {suggestions.length > 0 ? (
                <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3">
                  <p className="text-sm text-amber-800">
                    The carrier could not verify this address. Did you mean:
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {suggestions.map((suggestion, index) => (
                      <Button
                        key={index}
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => applySuggestion(suggestion)}
                      >
                        {suggestionParts(suggestion).join(", ")}
                      </Button>
                    ))}
                  </div>
                </div>
              ) : create.error ? (
                <p role="alert" className="text-sm text-red-600">
                  {mutationErrorMessage(create.error)}
                </p>
              ) : null}

              <div>
                <Button type="submit" disabled={create.isPending}>
                  {create.isPending ? "Adding address…" : "Add address"}
                </Button>
              </div>
            </form>
          ) : null}
        </div>
      </div>
    </Section>
  );
}
