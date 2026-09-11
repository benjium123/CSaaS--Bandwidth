import * as React from "react";
import { getErrorMessage } from "@/api/contacts";
import { useGate } from "@/api/capabilities";
import {
  MAX_ANNOUNCEMENT_LEN,
  MAX_DISPOSITIONS,
  MAX_DISPOSITION_LEN,
  normalizeDispositions,
  useCallingSettings,
  useUpdateCallingSettings,
  validateDispositions,
  type ChannelLayout,
} from "@/api/calls";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, MutationStatus, Section, Spinner, Textarea } from "@/components/ui/primitives";

export function SettingsCallingPage() {
  const { api } = useAuth();
  const gate = useGate();
  const canWrite = gate.can("settings:write");

  const calling = useCallingSettings(api);

  const layoutMutation = useUpdateCallingSettings(api);
  const announcementMutation = useUpdateCallingSettings(api);
  const dispositionsMutation = useUpdateCallingSettings(api);

  const [layoutDraft, setLayoutDraft] = React.useState<ChannelLayout | null>(null);
  const [announcementEnabledDraft, setAnnouncementEnabledDraft] = React.useState<boolean | null>(null);
  const [announcementTextDraft, setAnnouncementTextDraft] = React.useState<string | null>(null);
  const [dispositionsDraft, setDispositionsDraft] = React.useState<string[] | null>(null);
  const [listTouched, setListTouched] = React.useState(false);

  React.useEffect(() => {
    if (calling.data && layoutDraft === null) {
      setLayoutDraft(calling.data.channel_layout);
    }
    if (calling.data && announcementEnabledDraft === null) {
      setAnnouncementEnabledDraft(calling.data.recording_announcement);
    }
    if (calling.data && announcementTextDraft === null) {
      setAnnouncementTextDraft(calling.data.recording_announcement_text ?? "");
    }
    if (calling.data && dispositionsDraft === null) {
      setDispositionsDraft([...calling.data.dispositions]);
    }
  }, [calling.data, layoutDraft, announcementEnabledDraft, announcementTextDraft, dispositionsDraft]);

  if (calling.isLoading) {
    return <Spinner label="Loading calling settings" />;
  }

  if (calling.isError) {
    return (
      <div className="space-y-3">
        <p role="alert" className="text-sm text-destructive">
          {getErrorMessage(calling.error)}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void calling.refetch()}
        >
          Retry
        </Button>
      </div>
    );
  }

  if (!calling.data) return null;

  const settings = calling.data;
  const layoutValue = layoutDraft ?? settings.channel_layout;
  const announcementEnabledValue =
    announcementEnabledDraft ?? settings.recording_announcement;
  const announcementTextValue = announcementTextDraft ?? "";
  const listValue = dispositionsDraft ?? settings.dispositions;

  function saveLayout(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canWrite || layoutValue === settings.channel_layout || layoutMutation.isPending) {
      return;
    }
    layoutMutation.mutate(
      { channel_layout: layoutValue },
      { onSuccess: (data) => setLayoutDraft(data.channel_layout) },
    );
  }

  const normalizedAnnouncementText = announcementTextValue.trim() || null;
  const announcementTooLong =
    announcementTextValue.trim().length > MAX_ANNOUNCEMENT_LEN;
  const savedAnnouncementText = settings.recording_announcement_text ?? null;
  const announcementChanged =
    announcementEnabledValue !== settings.recording_announcement ||
    normalizedAnnouncementText !== savedAnnouncementText;

  function saveAnnouncement(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !canWrite ||
      announcementTooLong ||
      !announcementChanged ||
      announcementMutation.isPending
    ) {
      return;
    }

    const patch: {
      recording_announcement?: boolean;
      recording_announcement_text?: string | null;
    } = {};
    if (announcementEnabledValue !== settings.recording_announcement) {
      patch.recording_announcement = announcementEnabledValue;
    }
    if (normalizedAnnouncementText !== savedAnnouncementText) {
      patch.recording_announcement_text = normalizedAnnouncementText;
    }

    announcementMutation.mutate(patch, {
      onSuccess: (data) => {
        setAnnouncementEnabledDraft(data.recording_announcement);
        setAnnouncementTextDraft(data.recording_announcement_text ?? "");
      },
    });
  }

  const listValidationMessage = validateDispositions(listValue);
  const normalizedListDraft = normalizeDispositions(listValue);
  const listUnchanged =
    normalizedListDraft.length === settings.dispositions.length &&
    normalizedListDraft.every(
      (value, index) => value === settings.dispositions[index],
    );
  const listInvalid = listValidationMessage !== null;

  function handleListChange(index: number, value: string) {
    dispositionsMutation.reset();
    setDispositionsDraft((previous) => {
      const next = [...(previous ?? settings.dispositions)];
      next[index] = value;
      return next;
    });
    setListTouched(true);
  }

  function removeListRow(index: number) {
    dispositionsMutation.reset();
    setDispositionsDraft((previous) => {
      const next = [...(previous ?? settings.dispositions)];
      next.splice(index, 1);
      return next;
    });
    setListTouched(true);
  }

  function addListRow() {
    dispositionsMutation.reset();
    setDispositionsDraft((previous) => {
      const next = [...(previous ?? settings.dispositions)];
      next.push("");
      return next;
    });
    setListTouched(true);
  }

  function saveDispositions(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !canWrite ||
      listInvalid ||
      listUnchanged ||
      dispositionsMutation.isPending
    ) {
      return;
    }

    dispositionsMutation.mutate(
      { dispositions: normalizedListDraft },
      {
        onSuccess: (data) => {
          setDispositionsDraft([...data.dispositions]);
          setListTouched(false);
        },
      },
    );
  }

  return (
    <div className="space-y-6">
      {!canWrite ? (
        <p className="text-xs text-muted-foreground">
          You can view this, but only an admin can make changes here.
        </p>
      ) : null}

      <fieldset disabled={!canWrite} className="m-0 min-w-0 border-0 p-0">
        <Section title="Recording">
          <form onSubmit={saveLayout} className="space-y-3">
            {/* The helper is a description, not part of the name: a screen reader hears
                "One file with both sides, radio button" and then the explanation. */}
            <div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="channel-layout"
                  value="mixed"
                  aria-describedby="channel-layout-mixed-help"
                  checked={layoutValue === "mixed"}
                  onChange={() => {
                    layoutMutation.reset();
                    setLayoutDraft("mixed");
                  }}
                  disabled={!canWrite}
                />
                One file with both sides
              </label>
              <p id="channel-layout-mixed-help" className="pl-6 text-xs text-muted-foreground">
                Everyone on the call in a single recording.
              </p>
            </div>

            {/* The helper is a description, not part of the name: a screen reader hears
                "Separate sides, radio button" and then the explanation. */}
            <div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="channel-layout"
                  value="dual"
                  aria-describedby="channel-layout-dual-help"
                  checked={layoutValue === "dual"}
                  onChange={() => {
                    layoutMutation.reset();
                    setLayoutDraft("dual");
                  }}
                  disabled={!canWrite}
                />
                Separate sides
              </label>
              <p id="channel-layout-dual-help" className="pl-6 text-xs text-muted-foreground">
                Your side and their side as separate files, plus the combined one.
              </p>
            </div>

            <p className="text-xs text-muted-foreground">
              Separate sides isn't being captured yet. You can choose it now, but
              every recording is still saved as one file with both sides until it is
              switched on.
            </p>

            <div className="flex items-center gap-3">
              <Button
                type="submit"
                size="sm"
                aria-label="Save recording"
                disabled={
                  !canWrite ||
                  layoutValue === settings.channel_layout ||
                  layoutMutation.isPending
                }
              >
                Save
              </Button>
              <MutationStatus
                pending={layoutMutation.isPending}
                error={layoutMutation.error}
                success="Saved."
              />
            </div>
          </form>
        </Section>

        <Section title="Recording announcement">
          <form onSubmit={saveAnnouncement} className="space-y-3">
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={announcementEnabledValue}
                onChange={() => {
                  announcementMutation.reset();
                  setAnnouncementEnabledDraft(!announcementEnabledValue);
                }}
                disabled={!canWrite}
              />
              <span>Play an announcement before the call connects</span>
            </label>

            <Textarea
              aria-label="Announcement"
              maxLength={MAX_ANNOUNCEMENT_LEN}
              rows={3}
              value={announcementTextValue}
              onChange={(event) => {
                announcementMutation.reset();
                setAnnouncementTextDraft(event.target.value);
              }}
              placeholder={
                settings.recording_announcement_text === null
                  ? settings.announcement_text_effective
                  : ""
              }
              disabled={!canWrite}
            />

            <p className="text-xs text-muted-foreground">
              {settings.recording_announcement_text === null
                ? `Leave empty to use the standard sentence: “${settings.announcement_text_effective}”`
                : "Leave empty to go back to the standard sentence."}
            </p>

            <p className="text-xs text-muted-foreground">
              {announcementTextValue.length}/500
            </p>

            <p className="text-xs text-muted-foreground">
              It plays before incoming calls connect. Calls you place from this app
              don't play it yet, so tell the other person yourself.
            </p>

            <div className="space-y-1 rounded-md border border-border p-3">
              <p className="text-xs font-medium">
                Where everyone on the call must agree
              </p>
              <p className="text-xs text-muted-foreground">(not legal advice)</p>
              <p className="text-xs text-muted-foreground">
                California, Connecticut, Delaware, Florida, Illinois, Maryland,
                Massachusetts, Michigan, Montana, Nevada, New Hampshire, Oregon,
                Pennsylvania and Washington.
              </p>
              <p className="text-xs text-muted-foreground">
                Laws change and have exceptions. Check with a lawyer for your
                situation.
              </p>
            </div>

            {announcementTooLong ? (
              <p role="alert" className="text-sm text-destructive">
                Announcement can be at most 500 characters
              </p>
            ) : null}

            <div className="flex items-center gap-3">
              <Button
                type="submit"
                size="sm"
                aria-label="Save announcement"
                disabled={
                  !canWrite ||
                  announcementTooLong ||
                  !announcementChanged ||
                  announcementMutation.isPending
                }
              >
                Save
              </Button>
              <MutationStatus
                pending={announcementMutation.isPending}
                error={announcementMutation.error}
                success="Saved."
              />
            </div>
          </form>
        </Section>

        <Section title="Call results">
          <form onSubmit={saveDispositions} className="space-y-3">
            <p className="text-sm text-muted-foreground">
              The choices your team picks from after a call. Changing this list
              doesn't change results already saved on past calls.
            </p>

            <ol aria-label="Call results" className="space-y-2">
              {listValue.map((value, index) => (
                <li key={index} className="flex items-center gap-2">
                  <Input
                    aria-label={`Call result ${index + 1}`}
                    maxLength={MAX_DISPOSITION_LEN}
                    value={value}
                    onChange={(event) => handleListChange(index, event.target.value)}
                    disabled={!canWrite}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove call result ${index + 1}`}
                    onClick={() => removeListRow(index)}
                    disabled={!canWrite || listValue.length === 1 || dispositionsMutation.isPending}
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ol>

            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={addListRow}
              disabled={!canWrite || listValue.length >= MAX_DISPOSITIONS || dispositionsMutation.isPending}
            >
              Add a result
            </Button>

            {listTouched && listValidationMessage ? (
              <p role="alert" className="text-sm text-destructive">
                {listValidationMessage}
              </p>
            ) : null}

            <div className="flex items-center gap-3">
              <Button
                type="submit"
                size="sm"
                aria-label="Save call results"
                disabled={
                  !canWrite ||
                  listInvalid ||
                  listUnchanged ||
                  dispositionsMutation.isPending
                }
              >
                Save
              </Button>
              <MutationStatus
                pending={dispositionsMutation.isPending}
                error={dispositionsMutation.error}
                success="Saved."
              />
            </div>
          </form>
        </Section>
      </fieldset>
    </div>
  );
}
