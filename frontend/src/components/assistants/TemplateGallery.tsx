import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import type { Assistant } from "@/api/assistants";
import {
  TEMPLATES_KEY,
  createFromTemplate,
  emptyInterview,
  listTemplates,
  normalizeInterview,
  type Interview,
} from "@/api/agentTemplates";
import {
  Button,
  Input,
  MutationStatus,
  Pill,
  Spinner,
} from "@/components/ui/primitives";
import { SurfaceCard } from "@/components/ui/consoleChrome";

export const UPGRADE_TEXT = "AI assistants are included in the Team plan and above.";

export function UpgradeBanner() {
  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-[11px] rounded-[14px] border border-[hsl(var(--cx-flag)/0.35)] bg-[hsl(var(--cx-flag)/0.1)] p-[13px] text-[13px] text-[hsl(var(--cx-text))]"
    >
      <span>{UPGRADE_TEXT}</span>
      <a
        href="/settings?tab=billing"
        className="rounded-[10px] bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground"
      >
        Upgrade to Team
      </a>
    </div>
  );
}

/**
 * New-assistant flow: pick a ready-made template (or Start blank), name it, create it.
 * The interview inside a template is the starting point; the Setup tab edits it afterwards.
 */
export function TemplateGallery({
  locked,
  onCancel,
  onCreated,
}: {
  locked: boolean;
  onCancel: () => void;
  onCreated: (assistant: Assistant) => void;
}) {
  const { api } = useAuth();
  const templatesQuery = useQuery({
    queryKey: TEMPLATES_KEY,
    queryFn: () => listTemplates(api),
  });
  // null id = "Start blank": an empty interview, still created through the same endpoint.
  const [picked, setPicked] = React.useState<{ id: string | null; name: string; interview: Interview } | null>(null);
  const [name, setName] = React.useState("");

  const createMutation = useMutation({
    mutationFn: (template: { id: string | null; interview: Interview }) =>
      createFromTemplate(api, {
        template_id: template.id,
        name: name.trim(),
        interview: normalizeInterview(template.interview),
      }),
    onSuccess: (created) => onCreated(created),
  });

  if (picked) {
    return (
      <SurfaceCard className="max-w-xl space-y-[14px]">
        <h2 className="text-[16px] font-semibold text-[hsl(var(--cx-text))]">Name your assistant</h2>
        <p className="text-[13px] text-[hsl(var(--cx-muted))]">
          {picked.id ? `Starting from ${picked.name}.` : "Starting blank."} You can change everything afterwards.
        </p>
        <form
          className="space-y-[14px]"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim() || locked) return;
            createMutation.mutate(picked);
          }}
        >
          <div className="space-y-1">
            <label htmlFor="new-assistant-name" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
              Assistant name
            </label>
            <Input
              id="new-assistant-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Front desk"
              autoFocus
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={!name.trim() || locked || createMutation.isPending}>
              Create assistant
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setPicked(null);
                createMutation.reset();
              }}
            >
              Back
            </Button>
            <MutationStatus pending={createMutation.isPending} error={createMutation.error} pendingLabel="Creating…" />
          </div>
        </form>
      </SurfaceCard>
    );
  }

  const templates = templatesQuery.data ?? [];

  return (
    <div className="space-y-[14px]">
      <div className="flex flex-wrap items-center justify-between gap-[11px]">
        <h2 className="text-[16px] font-semibold text-[hsl(var(--cx-text))]">Start from a ready-made assistant</h2>
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {locked && <UpgradeBanner />}
      {templatesQuery.isLoading ? (
        <Spinner label="Loading templates" />
      ) : templatesQuery.isError ? (
        <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
          Templates are unavailable. You can still start blank.
        </p>
      ) : null}
      <ul aria-label="Templates" className="grid gap-[12px] [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]">
        <li>
          <button
            type="button"
            disabled={locked}
            onClick={() => {
              setName("");
              setPicked({ id: null, name: "Blank", interview: emptyInterview() });
            }}
            className="h-full w-full rounded-[14px] border border-dashed border-[hsl(var(--cx-line))] p-[14px] text-left disabled:opacity-50"
          >
            <div className="text-[14px] font-semibold text-[hsl(var(--cx-text))]">Start blank</div>
            <p className="mt-1 text-[12.5px] text-[hsl(var(--cx-muted))]">
              An empty interview. Fill in your own answers.
            </p>
          </button>
        </li>
        {templates.map((t) => {
          const off = locked || !t.available;
          return (
            <li key={t.id}>
              <button
                type="button"
                disabled={off}
                aria-label={t.name}
                onClick={() => {
                  setName("");
                  setPicked(t);
                }}
                className={`h-full w-full rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-[14px] text-left ${
                  off ? "opacity-50" : "hover:bg-[hsl(var(--cx-overlay))]"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="text-[14px] font-semibold text-[hsl(var(--cx-text))]">{t.name}</span>
                  <Pill tone="info">{t.channel === "sms" ? "SMS" : "Voice"}</Pill>
                </div>
                <p className="mt-1 text-[12.5px] text-[hsl(var(--cx-muted))]">{t.summary}</p>
                {!t.available && t.unavailable_reason && (
                  <p className="mt-2 text-[12px] font-semibold text-[hsl(var(--cx-flag))]">{t.unavailable_reason}</p>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
