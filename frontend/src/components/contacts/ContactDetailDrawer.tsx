/**
 * The per-contact drawer (Phase 27): possible duplicates with a merge preview, a copy of
 * everything we hold about this person, and erasure.
 *
 * Merge and erase are gated on the permissions the backend routes actually require -
 * contacts:write for POST /contacts/{id}/merge, compliance:manage for the erase route -
 * and erase is ABSENT rather than disabled for anyone else: offering a control that can
 * only ever 403 is worse than not offering it.
 */
import * as React from "react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { getErrorMessage, type ContactOut } from "@/api/contacts";
import {
  downloadTextFile,
  duplicateReasonLabel,
  fetchMyDataBundle,
  useContactDuplicates,
  useEraseContact,
  useMergeContacts,
} from "@/api/contactsPro";
import { Button, Drawer, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

export function ContactDetailDrawer({
  contact,
  onClose,
}: {
  contact: ContactOut;
  onClose: () => void;
}) {
  const { api, me, orgId } = useAuth();
  const canMerge = hasPermission(me, orgId, "contacts:write");
  const canErase = hasPermission(me, orgId, "compliance:manage");

  const duplicates = useContactDuplicates(api, contact.id);
  const mergeMutation = useMergeContacts(api);
  const eraseMutation = useEraseContact(api);

  const [selectedIds, setSelectedIds] = React.useState<string[]>([]);
  const [downloadError, setDownloadError] = React.useState<string | null>(null);
  const [eraseConfirming, setEraseConfirming] = React.useState(false);
  const [erasePhrase, setErasePhrase] = React.useState("");

  const selectedCandidates = (duplicates.data ?? []).filter((candidate) =>
    selectedIds.includes(candidate.contact_id),
  );
  const mergeCount = selectedIds.length;
  const eraseReady = erasePhrase.trim().toUpperCase() === "ERASE";

  const toggleCandidate = (candidateId: string) => {
    // A previous "Merged." line above a fresh selection would describe the wrong merge.
    mergeMutation.reset();
    setSelectedIds((current) =>
      current.includes(candidateId)
        ? current.filter((id) => id !== candidateId)
        : [...current, candidateId],
    );
  };

  const downloadData = async (format: "json" | "csv") => {
    setDownloadError(null);
    try {
      const payload = await fetchMyDataBundle(api, contact.id);
      if (format === "json") {
        downloadTextFile(
          `contact-${contact.id}.json`,
          JSON.stringify(payload.bundle, null, 2),
          "application/json",
        );
      } else {
        downloadTextFile(`contact-${contact.id}.csv`, payload.csv, "text/csv");
      }
    } catch (err) {
      setDownloadError(getErrorMessage(err));
    }
  };

  return (
    <Drawer open onClose={onClose} title={contact.display_name}>
      <div className="space-y-6">
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Phone numbers</h3>
          {contact.phones.length === 0 ? (
            <p className="text-sm text-muted-foreground">No phone numbers</p>
          ) : (
            <p className="text-sm">
              {contact.phones.map((phone) => formatPhone(phone.e164)).join(", ")}
            </p>
          )}
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Possible duplicates</h3>

          {duplicates.isLoading ? <Spinner label="Looking for duplicates" /> : null}
          {duplicates.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {getErrorMessage(duplicates.error)}
            </p>
          ) : null}
          {duplicates.data && duplicates.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">No duplicates found.</p>
          ) : null}

          {duplicates.data && duplicates.data.length > 0 ? (
            <div className="space-y-2">
              {duplicates.data.map((candidate) => (
                <div
                  key={candidate.contact_id}
                  className="flex items-start gap-2 rounded-md border border-border p-2"
                >
                  <input
                    type="checkbox"
                    aria-label={"Merge " + candidate.display_name}
                    title={
                      !canMerge
                        ? "You do not have permission to merge contacts."
                        : undefined
                    }
                    disabled={!canMerge}
                    checked={selectedIds.includes(candidate.contact_id)}
                    onChange={() => toggleCandidate(candidate.contact_id)}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">{candidate.display_name}</p>
                    <p className="text-sm text-muted-foreground">
                      {candidate.phones.length > 0
                        ? candidate.phones.map((phone) => formatPhone(phone)).join(", ")
                        : "No phone numbers"}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {duplicateReasonLabel(candidate.reason)}
                    </p>
                  </div>
                </div>
              ))}

              {selectedIds.length > 0 ? (
                <div className="space-y-2 rounded-md border border-border p-3">
                  <h4 className="text-sm font-medium">
                    If you merge, this is what you get
                  </h4>
                  <p className="text-sm">Kept: {contact.display_name}</p>
                  <p className="text-sm">
                    Merged away and hidden everywhere:{" "}
                    {selectedCandidates
                      .map((candidate) => candidate.display_name)
                      .join(", ")}
                  </p>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                    <li>
                      Their phone numbers, notes, conversations, tags and list
                      memberships all move onto {contact.display_name}.
                    </li>
                    <li>
                      Where both records have something different - a name, a
                      company, a custom field - {contact.display_name}&apos;s
                      version wins. Anything {contact.display_name} is missing is
                      filled in from the others.
                    </li>
                    <li>This cannot be undone.</li>
                  </ul>
                </div>
              ) : null}

              {canMerge && selectedIds.length > 0 ? (
                <Button
                  type="button"
                  disabled={mergeMutation.isPending}
                  onClick={() =>
                    mergeMutation.mutate(
                      { contactId: contact.id, loserIds: selectedIds },
                      { onSuccess: () => setSelectedIds([]) },
                    )
                  }
                >
                  {mergeMutation.isPending
                    ? "Merging…"
                    : mergeCount === 1
                      ? "Merge 1 contact"
                      : `Merge ${mergeCount} contacts`}
                </Button>
              ) : null}
            </div>
          ) : null}

          {mergeMutation.isSuccess ? (
            <p role="status" className="text-sm text-muted-foreground">
              Merged. {mergeMutation.data.merged.phones_moved} phone numbers,{" "}
              {mergeMutation.data.merged.threads_moved} conversations,{" "}
              {mergeMutation.data.merged.notes_moved} notes,{" "}
              {mergeMutation.data.merged.tags_moved} tags and{" "}
              {mergeMutation.data.merged.list_rows_moved} list rows moved onto{" "}
              {contact.display_name}.
            </p>
          ) : null}
          {mergeMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {getErrorMessage(mergeMutation.error)}
            </p>
          ) : null}
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold">
            Download this person&apos;s data
          </h3>
          <p className="text-sm text-muted-foreground">
            Everything we hold about this person, in one file.
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void downloadData("json")}
            >
              Download data (JSON)
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void downloadData("csv")}
            >
              Download data (CSV)
            </Button>
          </div>
          {downloadError ? (
            <p role="alert" className="text-sm text-destructive">
              {downloadError}
            </p>
          ) : null}
        </section>

        {canErase ? (
          <section className="space-y-2">
            <h3 className="text-sm font-semibold text-destructive">
              Erase this person
            </h3>
            <p className="text-sm text-muted-foreground">
              This permanently erases the personal data of this contact, but
              keeps their opt-out and consent records.
            </p>

            {eraseMutation.isSuccess ? (
              <p role="status" className="text-sm text-muted-foreground">
                {eraseMutation.data.message}
              </p>
            ) : eraseConfirming ? (
              <div className="space-y-3 rounded-md border border-destructive p-3">
                <h4 className="text-sm font-medium">
                  Erase {contact.display_name}? This cannot be undone.
                </h4>

                <div className="space-y-1">
                  <p className="text-sm font-medium">What we delete</p>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                    <li>
                      Their name and details (replaced with &quot;Erased
                      contact&quot;).
                    </li>
                    <li>Their phone numbers.</li>
                    <li>
                      The text of every message they sent or received (replaced
                      with &quot;[erased]&quot;).
                    </li>
                    <li>Call recordings, transcripts and notes.</li>
                  </ul>
                </div>

                <div className="space-y-1">
                  <p className="text-sm font-medium">What we keep, and why</p>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                    <li>
                      Their opt-out and do-not-call records stay, so this person
                      is never contacted again by mistake.
                    </li>
                  </ul>
                </div>

                <Input
                  aria-label="Type ERASE to confirm"
                  value={erasePhrase}
                  onChange={(event) => setErasePhrase(event.target.value)}
                />

                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="destructive"
                    disabled={!eraseReady || eraseMutation.isPending}
                    onClick={() =>
                      eraseMutation.mutate({ contactId: contact.id })
                    }
                  >
                    {eraseMutation.isPending ? "Erasing…" : "Erase permanently"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => {
                      setEraseConfirming(false);
                      setErasePhrase("");
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => setEraseConfirming(true)}
              >
                Erase this person
              </Button>
            )}

            {eraseMutation.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {getErrorMessage(eraseMutation.error)}
              </p>
            ) : null}
          </section>
        ) : null}
      </div>
    </Drawer>
  );
}
