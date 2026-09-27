import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getErrorMessage } from "@/api/contacts";
import { useAuth } from "@/auth/AuthContext";
import { Button, Card, Input, Pill, Spinner, type PillTone } from "@/components/ui/primitives";
import {
  EMPTY_ADDRESS_DRAFT,
  EmergencyAddressForm,
  draftToInput,
  isAddressDraftValid,
  type AddressDraft,
} from "@/components/numbers/EmergencyAddressForm";
import { formatPhone } from "@/lib/format";

/* "My profile": display name and the E911 address that rides with the caller's numbers. */

interface ProfileNumber {
  e164: string;
  status: string | null;
  mine: boolean;
}

interface EmergencyAddressRecord {
  id: string;
  name: string;
  street_address: string;
  extended_address: string | null;
  locality: string;
  administrative_area: string;
  postal_code: string;
  country_code: string;
  label: string;
}

interface Profile {
  full_name: string;
  email: string;
  emergency_address: EmergencyAddressRecord | null;
  numbers: ProfileNumber[];
  notice: string;
}

const PROFILE_QUERY_KEY = ["me", "profile"];

function statusTone(status: string | null): PillTone {
  switch (status) {
    case "active":
      return "success";
    case "pending":
    case "provisioning":
      return "warning";
    case "suspended":
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

function recordToDraft(address: EmergencyAddressRecord): AddressDraft {
  return {
    name: address.name,
    street_address: address.street_address,
    extended_address: address.extended_address ?? "",
    locality: address.locality,
    administrative_area: address.administrative_area,
    postal_code: address.postal_code,
  };
}

export function SettingsProfilePage(): JSX.Element {
  const { api } = useAuth();
  const qc = useQueryClient();

  const profile = useQuery({
    queryKey: PROFILE_QUERY_KEY,
    queryFn: () => api.request<Profile>("/api/v1/me/profile"),
  });

  // `name` stays null until the person types, so the input seeds itself from the server value
  // exactly once instead of fighting the user on every background refetch.
  const [name, setName] = useState<string | null>(null);
  const [nameSaved, setNameSaved] = useState(false);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<AddressDraft>(EMPTY_ADDRESS_DRAFT);
  const [applied, setApplied] = useState<number | null>(null);

  const saveName = useMutation({
    mutationFn: (full_name: string) =>
      api.request("/api/v1/me/profile", { method: "PUT", json: { full_name } }),
    onSuccess: () => {
      setNameSaved(true);
      void qc.invalidateQueries({ queryKey: PROFILE_QUERY_KEY });
    },
  });

  const saveAddress = useMutation({
    mutationFn: (value: AddressDraft) =>
      api.request<{ applied: number }>("/api/v1/me/emergency-address", {
        method: "PUT",
        json: draftToInput(value),
      }),
    onSuccess: (result) => {
      setApplied(result.applied);
      setEditing(false);
      void qc.invalidateQueries({ queryKey: PROFILE_QUERY_KEY });
    },
  });

  if (profile.isLoading) return <Spinner label="Loading profile" />;
  if (!profile.data) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {getErrorMessage(profile.error)}
      </p>
    );
  }

  const data = profile.data;
  const address = data.emergency_address;

  const serverName = data.full_name;
  const nameValue = name ?? serverName;
  const trimmedName = nameValue.trim();
  const canSaveName =
    trimmedName.length > 0 && trimmedName !== serverName.trim() && !saveName.isPending;

  const startEditing = () => {
    setDraft(address ? recordToDraft(address) : { ...EMPTY_ADDRESS_DRAFT });
    setApplied(null);
    saveAddress.reset();
    setEditing(true);
  };

  const stopEditing = () => {
    setEditing(false);
    saveAddress.reset();
  };

  return (
    <div className="space-y-6">
      <Card className="space-y-3 p-4">
        <h2 className="text-base font-semibold">Your name</h2>
        <Input
          aria-label="Your name"
          value={nameValue}
          onChange={(e) => {
            setName(e.target.value);
            setNameSaved(false);
          }}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" onClick={() => saveName.mutate(trimmedName)} disabled={!canSaveName}>
            {saveName.isPending ? "Saving…" : "Save"}
          </Button>
          {nameSaved ? <span className="text-sm text-green-600">Saved</span> : null}
        </div>
        {saveName.isError ? (
          <p role="alert" className="text-sm text-red-600">
            {getErrorMessage(saveName.error)}
          </p>
        ) : null}
      </Card>

      <Card className="space-y-3 p-4">
        <h2 className="text-base font-semibold">Emergency address (911)</h2>
        <p className="text-sm text-slate-600">
          Emergency calls from your phone numbers send this address to 911 dispatchers. Keep it
          where you actually work.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm">{address ? address.label : "Not set"}</span>
          {!editing ? (
            <Button type="button" variant="outline" onClick={startEditing}>
              {address ? "Edit" : "Set"}
            </Button>
          ) : null}
        </div>

        {editing ? (
          <div className="space-y-3">
            <EmergencyAddressForm
              value={draft}
              onChange={setDraft}
              disabled={saveAddress.isPending}
              idPrefix="settings-e911"
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                onClick={() => saveAddress.mutate(draft)}
                disabled={!isAddressDraftValid(draft) || saveAddress.isPending}
              >
                {saveAddress.isPending ? "Saving…" : "Save address"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={stopEditing}
                disabled={saveAddress.isPending}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : null}

        {applied !== null ? (
          <p className="text-sm text-green-600">{`Address registered on ${applied} number(s)`}</p>
        ) : null}

        {saveAddress.isError ? (
          <p role="alert" className="text-sm text-red-600">
            {getErrorMessage(saveAddress.error)}
          </p>
        ) : null}

        <div className="space-y-2 border-t border-slate-200 pt-3">
          <h3 className="text-sm font-medium">Numbers using your address</h3>
          {data.numbers.length === 0 ? (
            <p className="text-sm text-slate-500">
              No phone numbers are assigned to you yet. Ask an admin to add you to a line.
            </p>
          ) : (
            <ul className="space-y-1">
              {data.numbers.map((number) => (
                <li key={number.e164} className="flex flex-wrap items-center gap-2 text-sm">
                  <span>{formatPhone(number.e164)}</span>
                  <Pill tone={statusTone(number.status)}>{number.status ?? "not set"}</Pill>
                  {number.mine ? <span className="text-slate-500">(yours)</span> : null}
                </li>
              ))}
            </ul>
          )}
          {data.notice ? <p className="text-xs text-slate-500">{data.notice}</p> : null}
        </div>
      </Card>
    </div>
  );
}
