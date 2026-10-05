import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { ASSISTANTS_KEY, patchAssistant, type Assistant } from "@/api/assistants";
import {
  VOICES_KEY,
  listVoices,
  normalizeInterview,
  renderPrompt,
  type Interview,
  type PromptMode,
  type RenderedPrompt,
} from "@/api/agentTemplates";
import { Button, Input, MutationStatus, Select, Textarea } from "@/components/ui/primitives";
import { ConsoleCard, SurfaceCard } from "@/components/ui/consoleChrome";

const DEBOUNCE_MS = 400;
const LABEL = "block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]";

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      {children}
    </div>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset className="space-y-[10px] rounded-[14px] border border-[hsl(var(--cx-line))] p-[14px]">
      <legend className="px-1 text-[12.5px] font-semibold text-[hsl(var(--cx-text))]">{title}</legend>
      {children}
    </fieldset>
  );
}

function StringList({
  label,
  singular,
  items,
  onChange,
}: {
  label: string;
  singular: string;
  items: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={i} className="flex gap-2">
          <Input
            aria-label={`${label} ${i + 1}`}
            value={item}
            onChange={(e) => onChange(items.map((v, j) => (j === i ? e.target.value : v)))}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Remove ${label} ${i + 1}`}
            onClick={() => onChange(items.filter((_, j) => j !== i))}
          >
            Remove
          </Button>
        </div>
      ))}
      <Button type="button" variant="outline" size="sm" onClick={() => onChange([...items, ""])}>
        Add {singular}
      </Button>
    </div>
  );
}

const BUSINESS_FIELDS: [keyof Interview["business"], string][] = [
  ["name", "Business name"],
  ["type", "Business type"],
  ["hours", "Hours"],
  ["timezone", "Timezone"],
  ["address", "Address"],
  ["website", "Website"],
  ["service_area", "Service area"],
];

type Props = { assistant: Assistant; locked: boolean };

export function SetupTab({ assistant, locked }: Props) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const extra = (assistant.extra ?? {}) as Record<string, unknown>;
  const templateId = typeof extra.template_id === "string" ? extra.template_id : null;

  const [interview, setInterview] = React.useState<Interview>(() =>
    normalizeInterview(extra.interview as Partial<Interview> | undefined),
  );
  const [mode, setMode] = React.useState<PromptMode>(extra.prompt_mode === "custom" ? "custom" : "interview");
  const [prompt, setPrompt] = React.useState(assistant.system_prompt);
  const [greeting, setGreeting] = React.useState(assistant.greeting);
  const [meta, setMeta] = React.useState<Pick<RenderedPrompt, "locked" | "missing">>({ locked: [], missing: [] });
  const [dirty, setDirty] = React.useState(false);
  const [confirmRegen, setConfirmRegen] = React.useState(false);
  const [renderError, setRenderError] = React.useState(false);

  const modeRef = React.useRef(mode);
  modeRef.current = mode;
  const seq = React.useRef(0);
  const first = React.useRef(true);

  const voicesQuery = useQuery({ queryKey: VOICES_KEY, queryFn: () => listVoices(api) });

  const runRender = React.useCallback(
    async (iv: Interview, force: boolean) => {
      const mine = ++seq.current;
      try {
        const out = await renderPrompt(api, { template_id: templateId, interview: iv });
        if (mine !== seq.current) return;
        setRenderError(false);
        setMeta({ locked: out.locked ?? [], missing: out.missing ?? [] });
        if (force || modeRef.current === "interview") {
          setPrompt(out.prompt);
          setGreeting(out.greeting);
        }
      } catch {
        if (mine === seq.current) setRenderError(true);
      }
    },
    [api, templateId],
  );

  React.useEffect(() => {
    const delay = first.current ? 0 : DEBOUNCE_MS;
    first.current = false;
    const t = setTimeout(() => void runRender(interview, false), delay);
    return () => clearTimeout(t);
  }, [interview, runRender]);

  function edit(fn: (iv: Interview) => Interview) {
    setDirty(true);
    setInterview((cur) => fn(cur));
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      patchAssistant(api, assistant.id, {
        system_prompt: prompt,
        greeting,
        voice_id: interview.agent.voice_id || assistant.voice_id,
        language: interview.agent.language || assistant.language,
        extra: { ...extra, interview, prompt_mode: mode },
      }),
    onSuccess: () => {
      setDirty(false);
      void queryClient.invalidateQueries({ queryKey: ASSISTANTS_KEY });
    },
  });

  const b = interview.business;
  const voices = voicesQuery.data ?? [];
  const idp = `setup-${assistant.id}`;

  return (
    <div className="grid gap-[14px] min-[1000px]:grid-cols-2">
      <div className="min-w-0 space-y-[14px]">
        <Block title="Business">
          {BUSINESS_FIELDS.map(([key, label]) => (
            <Field key={key} id={`${idp}-b-${key}`} label={label}>
              <Input
                id={`${idp}-b-${key}`}
                value={b[key]}
                onChange={(e) => edit((iv) => ({ ...iv, business: { ...iv.business, [key]: e.target.value } }))}
              />
            </Field>
          ))}
        </Block>

        <Block title="Agent">
          <Field id={`${idp}-a-name`} label="Agent name">
            <Input
              id={`${idp}-a-name`}
              value={interview.agent.name}
              onChange={(e) => edit((iv) => ({ ...iv, agent: { ...iv.agent, name: e.target.value } }))}
            />
          </Field>
          <Field id={`${idp}-a-voice`} label="Voice">
            <Select
              id={`${idp}-a-voice`}
              value={interview.agent.voice_id}
              onChange={(e) => edit((iv) => ({ ...iv, agent: { ...iv.agent, voice_id: e.target.value } }))}
            >
              <option value="">Default voice</option>
              {voices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name} ({v.gender}, {v.accent})
                </option>
              ))}
            </Select>
          </Field>
          <Field id={`${idp}-a-lang`} label="Language">
            <Input
              id={`${idp}-a-lang`}
              value={interview.agent.language}
              onChange={(e) => edit((iv) => ({ ...iv, agent: { ...iv.agent, language: e.target.value } }))}
            />
          </Field>
          <Field id={`${idp}-a-greeting`} label="Greeting">
            <Textarea
              id={`${idp}-a-greeting`}
              rows={2}
              value={interview.agent.greeting}
              onChange={(e) => edit((iv) => ({ ...iv, agent: { ...iv.agent, greeting: e.target.value } }))}
            />
          </Field>
        </Block>

        <Block title="Goal">
          <Field id={`${idp}-goal`} label="The one end goal of a conversation">
            <Textarea
              id={`${idp}-goal`}
              rows={2}
              value={interview.goal}
              onChange={(e) => edit((iv) => ({ ...iv, goal: e.target.value }))}
            />
          </Field>
        </Block>

        <Block title="Should do">
          <StringList
            label="Should do"
            singular="item"
            items={interview.should_do}
            onChange={(next) => edit((iv) => ({ ...iv, should_do: next }))}
          />
        </Block>

        <Block title="Never do">
          <StringList
            label="Never do"
            singular="rule"
            items={interview.never_do}
            onChange={(next) => edit((iv) => ({ ...iv, never_do: next }))}
          />
        </Block>

        <Block title="FAQs">
          {interview.faqs.map((f, i) => (
            <div key={i} className="space-y-2 rounded-[12px] bg-[hsl(var(--cx-overlay))] p-[10px]">
              <Input
                aria-label={`Question ${i + 1}`}
                placeholder="Question"
                value={f.q}
                onChange={(e) =>
                  edit((iv) => ({ ...iv, faqs: iv.faqs.map((x, j) => (j === i ? { ...x, q: e.target.value } : x)) }))
                }
              />
              <Textarea
                aria-label={`Answer ${i + 1}`}
                placeholder="Answer"
                rows={2}
                value={f.a}
                onChange={(e) =>
                  edit((iv) => ({ ...iv, faqs: iv.faqs.map((x, j) => (j === i ? { ...x, a: e.target.value } : x)) }))
                }
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Remove FAQ ${i + 1}`}
                onClick={() => edit((iv) => ({ ...iv, faqs: iv.faqs.filter((_, j) => j !== i) }))}
              >
                Remove
              </Button>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => edit((iv) => ({ ...iv, faqs: [...iv.faqs, { q: "", a: "" }] }))}
          >
            Add FAQ
          </Button>
        </Block>

        <Block title="Handoff">
          <p className={LABEL}>Transfer to a person when</p>
          <StringList
            label="Handoff when"
            singular="condition"
            items={interview.handoff.when}
            onChange={(next) => edit((iv) => ({ ...iv, handoff: { ...iv.handoff, when: next } }))}
          />
          <Field id={`${idp}-h-num`} label="Transfer number">
            <Input
              id={`${idp}-h-num`}
              value={interview.handoff.transfer_number}
              onChange={(e) =>
                edit((iv) => ({ ...iv, handoff: { ...iv.handoff, transfer_number: e.target.value } }))
              }
            />
          </Field>
        </Block>

        <Block title="Booking">
          <label className="flex items-center gap-2 text-[13px] text-[hsl(var(--cx-text))]">
            <input
              type="checkbox"
              checked={interview.booking.enabled}
              onChange={(e) => edit((iv) => ({ ...iv, booking: { ...iv.booking, enabled: e.target.checked } }))}
            />
            Take booking requests
          </label>
          {interview.booking.enabled && (
            <Field id={`${idp}-bk-rules`} label="Booking rules">
              <Textarea
                id={`${idp}-bk-rules`}
                rows={2}
                value={interview.booking.rules}
                onChange={(e) => edit((iv) => ({ ...iv, booking: { ...iv.booking, rules: e.target.value } }))}
              />
            </Field>
          )}
        </Block>

        <Block title="After the call">
          <label className="flex items-center gap-2 text-[13px] text-[hsl(var(--cx-text))]">
            <input
              type="checkbox"
              checked={interview.after_call.summary}
              onChange={(e) =>
                edit((iv) => ({ ...iv, after_call: { ...iv.after_call, summary: e.target.checked } }))
              }
            />
            Write a call summary
          </label>
          <p className={LABEL}>Fields to capture</p>
          <StringList
            label="Capture field"
            singular="field"
            items={interview.after_call.fields}
            onChange={(next) => edit((iv) => ({ ...iv, after_call: { ...iv.after_call, fields: next } }))}
          />
        </Block>
      </div>

      <div className="min-w-0 space-y-[10px] min-[1000px]:sticky min-[1000px]:top-0 min-[1000px]:self-start">
        <h3 className="text-[13px] font-semibold text-[hsl(var(--cx-text))]">Prompt</h3>

        {meta.locked.length > 0 && (
          <ConsoleCard aria-label="Locked platform rules" className="space-y-2 opacity-70">
            <p className={LABEL}>Added by Ringlite, not editable</p>
            {meta.locked.map((text, i) => (
              <p key={i} className="whitespace-pre-wrap text-[12px] text-[hsl(var(--cx-muted))]">
                {text}
              </p>
            ))}
          </ConsoleCard>
        )}

        {mode === "custom" && (
          <div
            role="status"
            className="flex flex-wrap items-center justify-between gap-2 rounded-[12px] border border-[hsl(var(--cx-flag)/0.35)] bg-[hsl(var(--cx-flag)/0.1)] p-[10px] text-[12.5px] text-[hsl(var(--cx-text))]"
          >
            <span>Custom prompt</span>
            {confirmRegen ? (
              <span className="flex items-center gap-2">
                <span>Overwrite your edits?</span>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => {
                    setConfirmRegen(false);
                    setMode("interview");
                    modeRef.current = "interview";
                    setDirty(true);
                    void runRender(interview, true);
                  }}
                >
                  Confirm regenerate
                </Button>
                <Button type="button" variant="outline" size="sm" onClick={() => setConfirmRegen(false)}>
                  Cancel
                </Button>
              </span>
            ) : (
              <Button type="button" variant="outline" size="sm" onClick={() => setConfirmRegen(true)}>
                Regenerate from interview
              </Button>
            )}
          </div>
        )}

        <Textarea
          aria-label="System prompt"
          rows={18}
          className="font-mono text-[12.5px]"
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
            setMode("custom");
            setDirty(true);
          }}
        />

        {renderError && (
          <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
            Could not refresh the prompt. Your edits are kept.
          </p>
        )}

        {meta.missing.length > 0 && (
          <div aria-label="Missing answers" className="text-[12.5px] text-[hsl(var(--cx-flag))]">
            <p className="font-semibold">Still missing</p>
            <ul className="list-disc pl-5">
              {meta.missing.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        )}

        <SurfaceCard className="flex flex-wrap items-center gap-2 p-[12px]">
          <Button type="button" disabled={locked || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            Save setup
          </Button>
          {dirty && <span className="text-[11.5px] text-[hsl(var(--cx-flag))]">Unsaved changes</span>}
          <MutationStatus pending={saveMutation.isPending} error={saveMutation.error} success="Saved" pendingLabel="Saving…" />
        </SurfaceCard>
      </div>
    </div>
  );
}
