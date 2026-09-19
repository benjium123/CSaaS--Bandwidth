import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  type Address,
  type KycDocument,
  type KycPerson,
  uploadKycDocument,
  useKycMutation,
} from "@/api/kyc";
import {
  Button,
  Input,
  Pill,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import { InitialsAvatar } from "@/components/ui/consoleChrome";

/** P43: an owner's current home address and the recent document that proves it. The AI
 * reads the upload within seconds and says here whether it was accepted, and why not. */

export const REVIEW_PILL: Record<
  string,
  { label: string; tone: "success" | "warning" | "danger" | "neutral" }
> = {
  pass: { label: "Accepted", tone: "success" },
  warn: { label: "Needs a closer look", tone: "warning" },
  fail: { label: "Not accepted", tone: "danger" },
  reviewing: { label: "Checking…", tone: "neutral" },
};

export function DocumentReview({ doc }: { doc: KycDocument }) {
  const pill =
    REVIEW_PILL[doc.review_status ?? "reviewing"] ?? REVIEW_PILL.reviewing;
  return (
    <span className="inline-flex flex-col gap-1">
      <Pill tone={pill.tone}>{pill.label}</Pill>
      {doc.review_message && (
        <span className="text-xs text-muted-foreground">
          {doc.review_message}
        </span>
      )}
    </span>
  );
}

function formatAddress(a: Address): string {
  return [a.line1, a.line2, a.city, a.region, a.postal_code, a.country]
    .filter(Boolean)
    .join(", ");
}

export function OwnerResidence({
  person,
  documents,
  editable,
}: {
  person: KycPerson;
  documents: KycDocument[];
  editable: boolean;
}) {
  const { api } = useAuth();
  const current = person.residential_address;
  const [editing, setEditing] = React.useState(!current);
  const [line1, setLine1] = React.useState(current?.line1 ?? "");
  const [city, setCity] = React.useState(current?.city ?? "");
  const [region, setRegion] = React.useState(current?.region ?? "");
  const [postal, setPostal] = React.useState(current?.postal_code ?? "");
  const [country, setCountry] = React.useState(current?.country ?? "US");
  const [file, setFile] = React.useState<File | null>(null);
  const proofs = documents.filter(
    (d) => d.kind === "proof_of_address" && d.person_id === person.id,
  );
  const reviewing = proofs.some(
    (d) => (d.review_status ?? "reviewing") === "reviewing",
  );

  const save = useKycMutation(api, () =>
    api.request(`/api/v1/kyc/persons/${person.id}/address`, {
      method: "PUT",
      json: {
        line1,
        city,
        region: region || null,
        postal_code: postal,
        country,
      },
    }),
  );
  const upload = useKycMutation(api, () =>
    uploadKycDocument(api, "proof_of_address", file as File, person.id),
  );
  const remove = useKycMutation(api, (id: string) =>
    api.request(`/api/v1/kyc/documents/${id}`, { method: "DELETE" }),
  );

  // While the AI is reading an upload, refresh a few times so the result appears by itself
  // (at most ~2 minutes - a review that takes longer shows up on the next visit).
  const refresh = useKycMutation(api, async () => undefined);
  React.useEffect(() => {
    if (!reviewing) return;
    let polls = 0;
    const timer = window.setInterval(() => {
      polls += 1;
      if (polls > 30) {
        window.clearInterval(timer);
        return;
      }
      refresh.mutate(undefined);
    }, 4000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewing]);

  return (
    <div className="space-y-[11px] rounded-[var(--cx-r-md,14px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px]">
      {/* A KYC person is a person, so the block leads with their face. The avatar is
          decorative — the name is right beside it in the heading it already had. */}
      <div className="flex items-center gap-[11px]">
        <InitialsAvatar name={person.full_name} seed={person.id} size="md" />
        <p className="min-w-0 text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">
          Where {person.is_you ? "you live" : `${person.full_name} lives`} now
        </p>
      </div>
      <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
        Your ID's address can be out of date, so we ask for your current home
        address and a recent document that shows it: a utility bill, bank or
        card statement, government or tax letter, or lease, dated in the last 90
        days.
      </p>
      {current && !editing ? (
        <div className="flex flex-wrap items-center gap-[11px] text-[13.5px]">
          <span>{formatAddress(current)}</span>
          {editable && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setEditing(true)}
            >
              Change
            </Button>
          )}
        </div>
      ) : editable ? (
        <form
          className="grid gap-[11px] sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(undefined, { onSuccess: () => setEditing(false) });
          }}
        >
          <Input
            aria-label={`Home street address for ${person.full_name}`}
            placeholder="Street address"
            value={line1}
            onChange={(e) => setLine1(e.target.value)}
          />
          <Input
            aria-label={`City for ${person.full_name}`}
            placeholder="City"
            value={city}
            onChange={(e) => setCity(e.target.value)}
          />
          <Input
            aria-label={`State or county for ${person.full_name}`}
            placeholder="State / county"
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          />
          <Input
            aria-label={`Postal code for ${person.full_name}`}
            placeholder="ZIP / postal code"
            value={postal}
            onChange={(e) => setPostal(e.target.value)}
          />
          <Input
            aria-label={`Country for ${person.full_name}`}
            placeholder="Country code, e.g. US, CA, GB"
            maxLength={2}
            value={country}
            onChange={(e) => setCountry(e.target.value.toUpperCase())}
          />
          <Button
            type="submit"
            size="sm"
            disabled={
              !line1.trim() ||
              !city.trim() ||
              !postal.trim() ||
              country.length !== 2 ||
              save.isPending
            }
          >
            Save address
          </Button>
        </form>
      ) : (
        <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">No address yet.</p>
      )}

      {proofs.length > 0 && (
        <ul className="space-y-[9px]">
          {proofs.map((d) => (
            <li
              key={d.id}
              className="flex flex-wrap items-start justify-between gap-[11px] rounded-[var(--cx-r-sm,12px)] bg-[hsl(var(--cx-surface))] px-[11px] py-[9px] text-[13.5px]"
            >
              <span className="truncate">{d.filename}</span>
              <DocumentReview doc={d} />
              {editable && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove ${d.filename}`}
                  onClick={() => remove.mutate(d.id)}
                >
                  Remove
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && current && (
        <div className="flex flex-wrap items-center gap-[11px]">
          <Input
            aria-label={`Proof of address for ${person.full_name}`}
            type="file"
            accept="application/pdf,image/jpeg,image/png"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <Button
            type="button"
            size="sm"
            disabled={!file || upload.isPending}
            onClick={() =>
              upload.mutate(undefined, { onSuccess: () => setFile(null) })
            }
          >
            Upload proof
          </Button>
        </div>
      )}
      {(save.isError || upload.isError || remove.isError) && (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(save.error ?? upload.error ?? remove.error)}
        </p>
      )}
    </div>
  );
}
