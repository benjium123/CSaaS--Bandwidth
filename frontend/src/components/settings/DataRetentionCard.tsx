/**
 * Settings → Workspace → Data (Phase 27).
 *
 * Four numbers, one meaning: how long this workspace keeps something before the sweeper
 * deletes it. An empty box is "forever" - the backend stores NULL for exactly that - so
 * every field also spells the saved answer out in words rather than leaving an empty box
 * to be read as "nothing set".
 */
import * as React from "react";
import {
  RETENTION_DEFAULTS,
  useRetentionPolicy,
  useUpdateRetentionPolicy,
  type RetentionPatch,
  type RetentionPolicy,
} from "@/api/contactsPro";
import { getErrorMessage } from "@/api/contacts";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { Button, Card, Input, Section, Spinner } from "@/components/ui/primitives";

const RETENTION_FIELDS: ReadonlyArray<{
  key: keyof RetentionPolicy;
  label: string;
  helper: string;
}> = [
  {
    key: "messages_days",
    label: "Message text",
    helper:
      "How long we keep the words of a text message. The message itself stays, so your counts and reports do not change.",
  },
  {
    key: "recordings_days",
    label: "Call recordings",
    helper: "How long we keep call recording audio.",
  },
  {
    key: "transcripts_days",
    label: "Call transcripts",
    helper: "How long we keep the written version of a call.",
  },
  {
    key: "imports_days",
    label: "Imported files",
    helper: "How long we keep the spreadsheet a contact list was imported from.",
  },
];

function savedValueText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Kept forever";
  return `Kept for ${value} days`;
}

export function DataRetentionCard() {
  const { api, me, orgId } = useAuth();
  const retention = useRetentionPolicy(api);
  const updateRetention = useUpdateRetentionPolicy(api);
  const canEdit = hasPermission(me, orgId, "settings:write");

  const [draft, setDraft] = React.useState<Record<string, string> | null>(null);

  React.useEffect(() => {
    if (retention.data && draft === null) {
      const policy = retention.data;
      setDraft({
        messages_days: policy.messages_days === null ? "" : String(policy.messages_days),
        recordings_days:
          policy.recordings_days === null ? "" : String(policy.recordings_days),
        transcripts_days:
          policy.transcripts_days === null ? "" : String(policy.transcripts_days),
        imports_days: policy.imports_days === null ? "" : String(policy.imports_days),
      });
    }
  }, [retention.data, draft]);

  const dirty = React.useMemo(() => {
    const policy = retention.data;
    if (!draft || !policy) return false;
    return RETENTION_FIELDS.some((field) => {
      const saved = policy[field.key];
      return draft[field.key] !== (saved === null ? "" : String(saved));
    });
  }, [draft, retention.data]);

  const invalidField = React.useMemo(() => {
    if (!draft) return undefined;
    return RETENTION_FIELDS.find((field) => {
      const value = draft[field.key];
      if (value === "") return false;
      const parsed = Number(value);
      return !Number.isInteger(parsed) || parsed <= 0;
    });
  }, [draft]);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const policy = retention.data;
    if (!draft || !policy || !canEdit || invalidField) return;

    const patch: RetentionPatch = {};
    RETENTION_FIELDS.forEach((field) => {
      const saved = policy[field.key];
      const raw = draft[field.key] ?? "";
      const next = raw === "" ? null : Number(raw);
      if (next !== saved) {
        patch[field.key] = next;
      }
    });

    if (Object.keys(patch).length === 0) return;
    updateRetention.mutate(patch);
  }

  return (
    <Section title="Data">
      <Card className="space-y-4 p-4">
        <p className="text-sm text-muted-foreground">
          How long this workspace keeps message text, recordings, transcripts and the files
          you imported. Deleting on a schedule is the point - once something is past its date
          it is gone for good.
        </p>

        {retention.isLoading ? (
          <Spinner label="Loading retention" />
        ) : retention.isError ? (
          <div className="space-y-3">
            <p role="alert" className="text-sm text-destructive">
              {getErrorMessage(retention.error)}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => retention.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Leave a box empty to keep something forever.
            </p>

            {RETENTION_FIELDS.map((field) => {
              const inputId = `retention-${field.key}`;
              const savedValue = retention.data?.[field.key];
              const defaultValue = RETENTION_DEFAULTS[field.key];

              return (
                <div key={field.key} className="space-y-1">
                  <label htmlFor={inputId} className="text-sm font-medium">
                    {field.label}
                  </label>
                  <div className="flex items-center gap-2">
                    <Input
                      id={inputId}
                      type="number"
                      min={1}
                      inputMode="numeric"
                      aria-label={`${field.label} days`}
                      className="w-28"
                      value={draft?.[field.key] ?? ""}
                      onChange={(event) => {
                        const value = event.target.value;
                        // A stale "Saved." over a box the user is still editing reads as
                        // a promise we have not kept yet.
                        updateRetention.reset();
                        setDraft((previous) => ({
                          ...(previous ?? {}),
                          [field.key]: value,
                        }));
                      }}
                      placeholder="Keep forever"
                      disabled={!canEdit}
                      title={
                        canEdit
                          ? undefined
                          : "Only an admin can change how long data is kept."
                      }
                    />
                    <span className="text-sm text-muted-foreground">
                      {savedValueText(savedValue)}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">{field.helper}</p>
                  <p className="text-xs text-muted-foreground">
                    Default:{" "}
                    {defaultValue === null
                      ? "keep forever"
                      : `${defaultValue} days`}
                  </p>
                </div>
              );
            })}

            {invalidField ? (
              <p role="alert" className="text-sm text-destructive">
                Keep {invalidField.label} for at least one day, or leave it empty to keep
                them forever.
              </p>
            ) : null}

            {canEdit ? (
              <div className="flex items-center gap-3">
                <Button
                  type="submit"
                  size="sm"
                  disabled={!dirty || updateRetention.isPending}
                >
                  {updateRetention.isPending ? "Saving…" : "Save"}
                </Button>
                {updateRetention.isSuccess ? (
                  <p role="status" className="text-sm text-muted-foreground">
                    Saved.
                  </p>
                ) : null}
                {updateRetention.isError ? (
                  <p role="alert" className="text-sm text-destructive">
                    {getErrorMessage(updateRetention.error)}
                  </p>
                ) : null}
              </div>
            ) : null}
          </form>
        )}
      </Card>
    </Section>
  );
}
