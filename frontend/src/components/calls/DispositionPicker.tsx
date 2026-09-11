import * as React from "react";
import type { ApiClient } from "@/api/client";
import { useGate } from "@/api/capabilities";
import {
  MAX_DISPOSITION_NOTE_LEN,
  useCallDispositions,
  useCanUseCallNumber,
  useSetCallDisposition,
} from "@/api/calls";
import { Button, MutationStatus, Pill, Select, Textarea } from "@/components/ui/primitives";

export function DispositionPicker({
  api,
  callId,
  ourE164,
  disposition,
  note,
}: {
  api: ApiClient;
  callId: string;
  ourE164: string;
  disposition: string | null;
  note: string | null;
}) {
  const gate = useGate();
  const canReadCatalogue = gate.can("calls:read");
  const { canUse } = useCanUseCallNumber(api, ourE164);
  const catalogue = useCallDispositions(api, canReadCatalogue);
  const setDisposition = useSetCallDisposition(api);

  const [expanded, setExpanded] = React.useState(false);
  const [selected, setSelected] = React.useState("");
  const [draftNote, setDraftNote] = React.useState("");

  const editable = Boolean(canReadCatalogue && canUse && catalogue.data);

  function openForm() {
    setDisposition.reset();
    setSelected(disposition ?? "");
    setDraftNote(note ?? "");
    setExpanded(true);
  }

  if (!editable) {
    if (!disposition) return null;

    return (
      <div role="group" aria-label="Call result" className="mt-2 space-y-2 text-sm">
        <Pill tone="info">{disposition}</Pill>
        {note ? (
          <p className="whitespace-pre-wrap text-xs text-muted-foreground">{note}</p>
        ) : null}
      </div>
    );
  }

  if (!expanded) {
    return (
      <div role="group" aria-label="Call result" className="mt-2 space-y-2 text-sm">
        {disposition ? (
          <>
            <Pill tone="info">{disposition}</Pill>
            {note ? (
              <p className="whitespace-pre-wrap text-xs text-muted-foreground">{note}</p>
            ) : null}
            <Button type="button" variant="ghost" size="sm" onClick={openForm}>
              Change
            </Button>
          </>
        ) : (
          <Button type="button" variant="outline" size="sm" onClick={openForm}>
            Add call result
          </Button>
        )}
      </div>
    );
  }

  const choices = catalogue.data?.dispositions ?? [];
  const staleValue =
    disposition != null && !choices.includes(disposition) ? disposition : null;
  const selectedIsStale = staleValue !== null && selected === staleValue;

  const normalizedDraftNote = draftNote.trim() || null;
  const normalizedSavedNote = note?.trim() || null;
  const nothingChanged =
    selected === (disposition ?? "") && normalizedDraftNote === normalizedSavedNote;
  const saveDisabled =
    setDisposition.isPending || nothingChanged || selectedIsStale;

  function handleSelectChange(event: React.ChangeEvent<HTMLSelectElement>) {
    setDisposition.reset();
    const next = event.target.value;
    setSelected(next);
    if (next === "") setDraftNote("");
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saveDisabled) return;

    setDisposition.mutate(
      {
        callId,
        disposition: selected || null,
        note: selected ? (draftNote.trim() || null) : null,
      },
      { onSuccess: () => setExpanded(false) },
    );
  }

  return (
    <div role="group" aria-label="Call result" className="mt-2 space-y-2 text-sm">
      <form onSubmit={handleSubmit} className="space-y-2">
        <Select aria-label="Call result" value={selected} onChange={handleSelectChange}>
          <option value="">No result</option>
          {choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
          {staleValue ? (
            <option value={staleValue} disabled>
              {staleValue} (no longer on the list)
            </option>
          ) : null}
        </Select>

        <Textarea
          aria-label="Note"
          placeholder="Add a note (optional)"
          maxLength={MAX_DISPOSITION_NOTE_LEN}
          rows={2}
          value={draftNote}
          onChange={(event) => {
            setDisposition.reset();
            setDraftNote(event.target.value);
          }}
          disabled={!selected}
        />

        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={saveDisabled}>
            Save
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setExpanded(false)}
          >
            Cancel
          </Button>
          <MutationStatus pending={setDisposition.isPending} error={setDisposition.error} />
        </div>
      </form>
    </div>
  );
}
