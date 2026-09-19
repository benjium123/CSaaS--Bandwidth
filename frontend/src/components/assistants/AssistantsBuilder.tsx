import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  ASSISTANTS_KEY,
  COMPLIANCE_PREAMBLE,
  composeEffectivePrompt,
  createAssistant,
  deleteAssistant,
  fetchAssistants,
  goLiveAssistant,
  goLiveBlockers,
  patchAssistant,
  setDefaultAssistant,
  type Assistant,
  type AssistantTool,
  type PostCallField,
} from "@/api/assistants";
import {
  BehaviourTab,
  OutcomesTab,
  ToolsTab,
} from "@/components/assistants/AssistantConfigTabs";
import { AssistantAnalyticsPanel } from "@/components/assistants/AssistantAnalytics";
import { CallMePanel } from "@/components/assistants/CallMePanel";
import { KnowledgeTab } from "@/components/assistants/KnowledgeTab";
import { SimulatorDrawer } from "@/components/assistants/SimulatorDrawer";
import { VoicePreviewButton } from "@/components/assistants/VoicePreviewButton";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  tabId,
  panelId,
  TabPanel,
  Tabs,
  Textarea,
} from "@/components/ui/primitives";
import {
  ConsoleCard,
  InitialsAvatar,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

export type AssistantForm = {
  name: string;
  greeting: string;
  language: string;
  voice_id: string;
  system_prompt: string;
  goals: string;
  guardrails: string;
  max_call_seconds: number;
  silence_timeout_seconds: number;
  interrupt_sensitivity: string;
  voicemail_action: string;
  tools: AssistantTool[];
  post_call_fields: PostCallField[];
  // The four texting settings the old agent form owned. They live on the Behaviour tab now;
  // dropping them would have silently removed the SMS agent's only editor.
  sms_enabled: boolean;
  sms_turn_ceiling: number;
  sms_max_reply_chars: number;
  sms_handoff_keywords: string[];
};

const EMPTY_FORM: AssistantForm = {
  name: "",
  greeting: "",
  language: "en",
  voice_id: "",
  system_prompt: "",
  goals: "",
  guardrails: "",
  max_call_seconds: 900,
  silence_timeout_seconds: 12,
  interrupt_sensitivity: "medium",
  voicemail_action: "leave_message",
  tools: [],
  post_call_fields: [],
  sms_enabled: false,
  sms_turn_ceiling: 10,
  sms_max_reply_chars: 480,
  sms_handoff_keywords: [],
};

function formFromAssistant(a: Assistant): AssistantForm {
  return {
    name: a.name,
    greeting: a.greeting,
    language: a.language ?? "en",
    voice_id: a.voice_id,
    system_prompt: a.system_prompt,
    goals: a.goals ?? "",
    guardrails: a.guardrails ?? "",
    max_call_seconds: a.max_call_seconds ?? 900,
    silence_timeout_seconds: a.silence_timeout_seconds ?? 12,
    interrupt_sensitivity: a.interrupt_sensitivity ?? "medium",
    voicemail_action: a.voicemail_action ?? "leave_message",
    tools: a.tools ?? [],
    post_call_fields: a.post_call_fields ?? [],
    sms_enabled: a.sms_enabled ?? false,
    sms_turn_ceiling: a.sms_turn_ceiling ?? 10,
    sms_max_reply_chars: a.sms_max_reply_chars ?? 480,
    sms_handoff_keywords: a.sms_handoff_keywords ?? [],
  };
}

/**
 * Item 47: a two-step "click again to confirm" destructive-action pattern - the first
 * click arms a "Confirm...?" state (with a Cancel escape hatch), the second click
 * within CONFIRM_TIMEOUT_MS actually performs the action. Left alone, it silently
 * reverts to the plain button so a stale armed state doesn't linger and get triggered
 * by an unrelated later click.
 */
const CONFIRM_TIMEOUT_MS = 8000;

function useConfirm() {
  const [confirming, setConfirming] = React.useState(false);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = React.useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  const requestConfirm = React.useCallback(() => {
    setConfirming(true);
    clear();
    timerRef.current = setTimeout(() => setConfirming(false), CONFIRM_TIMEOUT_MS);
  }, [clear]);

  const cancel = React.useCallback(() => {
    setConfirming(false);
    clear();
  }, [clear]);

  React.useEffect(() => clear, [clear]);

  return { confirming, requestConfirm, cancel };
}

type SaveMutationInput = {
  patch: Partial<Assistant>;
  create: boolean;
  action: string;
  fullForm: AssistantForm;
};

function BuilderSettingsTabs({
  id,
  tabs,
  value,
  onChange,
  ariaLabel,
  children,
}: {
  id: string;
  tabs: { id: string; label: React.ReactNode; disabled?: boolean }[];
  value: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <Tabs
        tabs={tabs}
        value={value}
        onChange={onChange}
        ariaLabel={ariaLabel}
        id={id}
      />
      {tabs.map((tab) =>
        tab.id === value ? (
          <TabPanel key={tab.id} tabsId={id} id={tab.id}>
            {children}
          </TabPanel>
        ) : (
          <div
            key={tab.id}
            role="tabpanel"
            hidden
            id={panelId(id, tab.id)}
            aria-labelledby={tabId(id, tab.id)}
          />
        ),
      )}
    </div>
  );
}

export function AssistantsBuilder() {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [form, setForm] = React.useState<AssistantForm>(EMPTY_FORM);
  const [dirty, setDirty] = React.useState(false);
  // P23a: tab is local React state, NOT a URL parameter. The surrounding SettingsPage
  // already owns ?tab= for the AI section's own tabs, and a second writer would fight it.
  const [tab, setTab] = React.useState("persona");
  const [goLiveBlockersList, setGoLiveBlockersList] = React.useState<string[]>([]);
  const [simulatorOpen, setSimulatorOpen] = React.useState(false);
  const [lastAction, setLastAction] = React.useState<
    "persona" | "instructions" | "tools" | "behaviour" | "outcomes" | null
  >(null);

  const deleteConfirm = useConfirm();

  const assistantsQuery = useQuery({
    queryKey: ASSISTANTS_KEY,
    queryFn: () => fetchAssistants(api),
  });

  const selected = (assistantsQuery.data ?? []).find((a) => a.id === selectedId) ?? null;

  // (a) Keyed on `selectedId` (a stable primitive), NOT on `selected` (a fresh object
  // reference on every background refetch of the assistants list). A refetch that leaves
  // the same assistant selected must never re-run this and clobber whatever the user is
  // mid-typing. A real switch, including to "New assistant" (selectedId -> null), always
  // resyncs and always wins over a stale dirty flag from the assistant just left.
  React.useEffect(() => {
    const next = selected ? formFromAssistant(selected) : EMPTY_FORM;
    setForm(next);
    setDirty(false);
    deleteConfirm.cancel();

    // If the active tab became disabled because the user clicked New while on Tools,
    // fall back to Persona. Persona is the only tab a brand-new assistant can save.
    setTab((current) => (selectedId === null ? "persona" : current));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const saveMutation = useMutation({
    mutationFn: async (variables: SaveMutationInput) => {
      if (variables.create) {
        return createAssistant(api, variables.fullForm);
      }
      if (!selectedId) {
        throw new Error("No assistant selected.");
      }
      return patchAssistant(api, selectedId, variables.patch);
    },
    onSuccess: async (saved, variables) => {
      setDirty(false);
      if (variables.create) {
        // (b) Make sure the just-created assistant is actually in the query cache before
        // selecting it. Otherwise `selected` resolves to null for a beat and the form
        // flashes back to EMPTY_FORM instead of showing what was just made.
        await queryClient.invalidateQueries({
          queryKey: ASSISTANTS_KEY,
          refetchType: "active",
        });
        setSelectedId(saved.id);
      } else {
        void queryClient.invalidateQueries({ queryKey: ASSISTANTS_KEY });
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteAssistant(api, id),
    onSuccess: (_saved, id) => {
      void queryClient.invalidateQueries({ queryKey: ASSISTANTS_KEY });
      if (selectedId === id) {
        setSelectedId(null);
        setForm(EMPTY_FORM);
        setDirty(false);
        setTab("persona");
        setGoLiveBlockersList([]);
      }
    },
  });

  const setDefaultMutation = useMutation({
    mutationFn: (id: string) => setDefaultAssistant(api, id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ASSISTANTS_KEY });
    },
  });

  const goLiveMutation = useMutation({
    mutationFn: (id: string) => goLiveAssistant(api, id),
    onError: (error: unknown) => {
      setGoLiveBlockersList(goLiveBlockers(error));
    },
    onSuccess: () => {
      setGoLiveBlockersList([]);
      void queryClient.invalidateQueries({ queryKey: ASSISTANTS_KEY });
    },
  });

  function startNew() {
    setSelectedId(null);
    setForm(EMPTY_FORM);
    setDirty(false);
    setGoLiveBlockersList([]);
    setSimulatorOpen(false);
    setTab("persona");
    deleteConfirm.cancel();
  }

  function updateField<K extends keyof AssistantForm>(key: K, value: AssistantForm[K]) {
    setDirty(true);
    setForm((current) => ({ ...current, [key]: value }));
  }

  function saveTab(patch: Partial<Assistant>, action: "persona" | "instructions" | "tools" | "behaviour" | "outcomes") {
    setLastAction(action);
    saveMutation.mutate({
      patch,
      create: selectedId === null,
      action,
      fullForm: form,
    });
  }

  function renderPersona() {
    return (
      <form
        className="space-y-[14px]"
        onSubmit={(event) => {
          event.preventDefault();
          saveTab(
            {
              name: form.name,
              greeting: form.greeting,
              language: form.language,
              voice_id: form.voice_id,
            },
            "persona",
          );
        }}
      >
        <div className="space-y-1">
          <label htmlFor="assistant-name" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Name
          </label>
          <Input
            id="assistant-name"
            aria-label="Assistant name"
            value={form.name}
            onChange={(event) => updateField("name", event.target.value)}
            required
          />
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-greeting" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Greeting
          </label>
          <Textarea
            id="assistant-greeting"
            rows={2}
            aria-label="Greeting"
            placeholder="Hi, thanks for calling — how can I help?"
            value={form.greeting}
            onChange={(event) => updateField("greeting", event.target.value)}
          />
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-language" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Language
          </label>
          <Select
            id="assistant-language"
            aria-label="Language"
            value={form.language}
            onChange={(event) => updateField("language", event.target.value)}
          >
            <option value="en">English</option>
            <option value="es">Spanish</option>
            <option value="fr">French</option>
            <option value="de">German</option>
            <option value="pt">Portuguese</option>
          </Select>
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-voice" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Voice id
          </label>
          <Input
            id="assistant-voice"
            aria-label="Voice id"
            value={form.voice_id}
            onChange={(event) => updateField("voice_id", event.target.value)}
          />
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
            The voice id from your voice provider.
          </p>
        </div>

        {/* The sample is generated from the SAVED voice provider but the TYPED voice id, so
            trying a different voice does not need a save first. */}
        <VoicePreviewButton
          ttsProvider={selected?.tts_provider ?? ""}
          voiceId={form.voice_id}
        />

        <div className="flex flex-wrap items-center gap-[11px]">
          <Button type="submit" disabled={!form.name.trim() || saveMutation.isPending}>
            {selectedId ? "Save persona" : "Create assistant"}
          </Button>
          {lastAction === "persona" && (
            <MutationStatus
              pending={saveMutation.isPending}
              error={saveMutation.error}
              success="Saved"
              pendingLabel="Saving…"
            />
          )}
        </div>
      </form>
    );
  }

  function renderInstructions() {
    return (
      <form
        className="space-y-[14px]"
        onSubmit={(event) => {
          event.preventDefault();
          saveTab(
            {
              system_prompt: form.system_prompt,
              goals: form.goals,
              guardrails: form.guardrails,
            },
            "instructions",
          );
        }}
      >
        <div className="space-y-1">
          <label htmlFor="assistant-instructions" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Instructions
          </label>
          <Textarea
            id="assistant-instructions"
            rows={8}
            aria-label="Instructions"
            value={form.system_prompt}
            onChange={(event) => updateField("system_prompt", event.target.value)}
          />
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
            How your assistant should behave.
          </p>
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-goals" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Goals
          </label>
          <Textarea
            id="assistant-goals"
            rows={4}
            aria-label="Goals"
            value={form.goals}
            onChange={(event) => updateField("goals", event.target.value)}
          />
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">What a good call achieves.</p>
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-guardrails" className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Guardrails
          </label>
          <Textarea
            id="assistant-guardrails"
            rows={4}
            aria-label="Guardrails"
            value={form.guardrails}
            onChange={(event) => updateField("guardrails", event.target.value)}
          />
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">What it must never do.</p>
        </div>

        <Section title="What your assistant actually gets" className="space-y-[11px]">
          <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
            The first paragraph is always included and cannot be changed.
          </p>
          <p className="rounded-[14px] bg-[hsl(var(--cx-overlay))] p-[13px] text-[12.5px] text-[hsl(var(--cx-subtle))]">
            {COMPLIANCE_PREAMBLE}
          </p>
          {/* The server's effective_prompt wins when present because the server owns the
              real merge of these fields. */}
          <Textarea
            readOnly
            aria-label="Full instructions"
            rows={10}
            value={selected?.effective_prompt ?? composeEffectivePrompt(form)}
          />
        </Section>

        <div className="flex flex-wrap items-center gap-[11px]">
          <Button type="submit" disabled={saveMutation.isPending}>
            Save instructions
          </Button>
          {lastAction === "instructions" && (
            <MutationStatus
              pending={saveMutation.isPending}
              error={saveMutation.error}
              success="Saved"
              pendingLabel="Saving…"
            />
          )}
        </div>
      </form>
    );
  }

  const tabs = [
    { id: "persona", label: "Persona", disabled: false },
    { id: "instructions", label: "Instructions", disabled: !selectedId },
    { id: "knowledge", label: "Knowledge", disabled: !selectedId },
    { id: "tools", label: "Tools", disabled: !selectedId },
    { id: "behaviour", label: "Behaviour", disabled: !selectedId },
    { id: "outcomes", label: "Outcomes", disabled: !selectedId },
  ];

  return (
    <div className="grid h-full grid-cols-[280px_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))]">
        <div className="flex items-center justify-between gap-[11px] border-b border-[hsl(var(--cx-line))] p-[14px]">
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
            Assistants
          </h1>
          <Button type="button" size="sm" onClick={startNew}>
            New
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-[9px]">
          {assistantsQuery.isLoading ? (
            <Spinner label="Loading assistants" />
          ) : assistantsQuery.isError ? (
            <p role="alert" className="p-[14px] text-[13px] text-[hsl(var(--cx-danger))]">
              Assistants are unavailable.
            </p>
          ) : (assistantsQuery.data ?? []).length === 0 ? (
            <EmptyState
              title="No assistants yet."
              description="Create one to answer your calls."
            />
          ) : (
            <>
              <SectionLabel className="px-[12px] pb-[7px] pt-[3px]">
                Your assistants
              </SectionLabel>
              <ul aria-label="Assistants" className="space-y-[3px]">
              {(assistantsQuery.data ?? []).map((assistant) => (
                <li key={assistant.id}>
                  {/* The reference's row: 12px radius, 12px padding, overlay when it is
                      the current one. */}
                  <Button
                    type="button"
                    variant="ghost"
                    aria-current={assistant.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(assistant.id)}
                    className={`h-auto w-full justify-between gap-[11px] rounded-[12px] px-[12px] py-[10px] text-left text-[13.5px] ${
                      assistant.id === selectedId
                        ? "bg-[hsl(var(--cx-overlay))] font-semibold text-[hsl(var(--cx-text))]"
                        : "text-[hsl(var(--cx-subtle))] hover:bg-[hsl(var(--cx-overlay))] hover:text-[hsl(var(--cx-text))]"
                    }`}
                  >
                    <span className="flex min-w-0 items-center gap-[11px]">
                      <InitialsAvatar
                        name={assistant.name}
                        seed={assistant.id}
                        size="sm"
                      />
                      <span className="truncate">{assistant.name}</span>
                    </span>
                    {assistant.is_default && <Pill tone="success">Default</Pill>}
                  </Button>
                </li>
              ))}
              </ul>
            </>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto p-[18px]">
        <div className="flex flex-wrap items-start justify-between gap-[11px]">
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
            {selected ? selected.name : "New assistant"}
          </h1>

          {selected && (
            <div className="flex flex-wrap items-center gap-2">
              {dirty && (
                <span className="text-[11.5px] text-[hsl(var(--cx-flag))]">Unsaved changes</span>
              )}
              {/* A simulated turn runs against the SAVED assistant, so offering this
                  mid-edit would answer with the wrong instructions. */}
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!selectedId || dirty}
                title={!selectedId || dirty ? "Save your changes first" : undefined}
                onClick={() => setSimulatorOpen(true)}
              >
                Test your assistant
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={goLiveMutation.isPending}
                onClick={() => goLiveMutation.mutate(selected.id)}
              >
                Go live
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={selected.is_default || setDefaultMutation.isPending}
                onClick={() => setDefaultMutation.mutate(selected.id)}
              >
                {selected.is_default ? "Default" : "Make default"}
              </Button>
              {deleteConfirm.confirming ? (
                <>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    onClick={() => {
                      deleteConfirm.cancel();
                      deleteMutation.mutate(selected.id);
                    }}
                    disabled={deleteMutation.isPending}
                  >
                    Confirm delete?
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={deleteConfirm.cancel}
                  >
                    Cancel
                  </Button>
                </>
              ) : (
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  onClick={deleteConfirm.requestConfirm}
                  disabled={deleteMutation.isPending}
                >
                  Delete
                </Button>
              )}
            </div>
          )}
        </div>

        {goLiveBlockersList.length > 0 && (
          <div
            role="alert"
            className="mt-[14px] space-y-1 rounded-[14px] border border-[hsl(var(--cx-danger)/0.35)] bg-[hsl(var(--cx-danger)/0.1)] p-[13px] text-[13px] text-[hsl(var(--cx-danger))]"
          >
            <p>Before this assistant can go live:</p>
            <ul aria-label="What is missing">
              {goLiveBlockersList.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        )}

        {goLiveMutation.status !== "idle" && (
          <MutationStatus
            className="mt-2"
            pending={goLiveMutation.isPending}
            /* When the blockers alert above is showing, it IS the error message - repeating
               the raw one underneath says the same thing twice, in worse words. */
            error={goLiveBlockersList.length > 0 ? undefined : goLiveMutation.error}
            success="This assistant is live."
            pendingLabel="Going live…"
          />
        )}

        {/* A real call is the only way to judge a voice assistant, so it sits above the
            settings rather than buried in a tab. */}
        <ConsoleCard className="mt-[14px]">
          <CallMePanel assistantId={selectedId} />
        </ConsoleCard>

        <div className="mt-[14px]">
          <BuilderSettingsTabs
            id="assistant-builder"
            tabs={tabs}
            value={tab}
            onChange={setTab}
            ariaLabel="Assistant settings"
          >
            <SurfaceCard className="max-w-2xl space-y-[14px]">
              {!selectedId && (
                <p className="text-[13px] text-[hsl(var(--cx-muted))]">
                  Save this assistant first, then you can set up the rest.
                </p>
              )}

              {tab === "persona" && renderPersona()}

              {tab === "instructions" && renderInstructions()}

              {tab === "knowledge" && (
                <>
                  <p className="text-[13px] text-[hsl(var(--cx-muted))]">
                    Your assistants share one knowledge library.
                  </p>
                  <KnowledgeTab />
                </>
              )}

              {tab === "tools" && (
                <ToolsTab
                  tools={form.tools}
                  onChange={(next) => updateField("tools", next)}
                  onSave={() => saveTab({ tools: form.tools }, "tools")}
                  saving={saveMutation.isPending && lastAction === "tools"}
                  status={
                    lastAction === "tools" ? (
                      <MutationStatus
                        pending={saveMutation.isPending}
                        error={saveMutation.error}
                        success="Saved"
                        pendingLabel="Saving…"
                      />
                    ) : null
                  }
                />
              )}

              {tab === "behaviour" && (
                <BehaviourTab
                  value={{
                    max_call_seconds: form.max_call_seconds,
                    silence_timeout_seconds: form.silence_timeout_seconds,
                    interrupt_sensitivity: form.interrupt_sensitivity,
                    voicemail_action: form.voicemail_action,
                    sms_enabled: form.sms_enabled,
                    sms_turn_ceiling: form.sms_turn_ceiling,
                    sms_max_reply_chars: form.sms_max_reply_chars,
                    sms_handoff_keywords: form.sms_handoff_keywords,
                  }}
                  onChange={(next) => {
                    setDirty(true);
                    setForm((current) => ({ ...current, ...next }));
                  }}
                  onSave={() =>
                    saveTab(
                      {
                        max_call_seconds: form.max_call_seconds,
                        silence_timeout_seconds: form.silence_timeout_seconds,
                        interrupt_sensitivity: form.interrupt_sensitivity,
                        voicemail_action: form.voicemail_action,
                        sms_enabled: form.sms_enabled,
                        sms_turn_ceiling: form.sms_turn_ceiling,
                        sms_max_reply_chars: form.sms_max_reply_chars,
                        sms_handoff_keywords: form.sms_handoff_keywords,
                      },
                      "behaviour",
                    )
                  }
                  saving={saveMutation.isPending && lastAction === "behaviour"}
                  status={
                    lastAction === "behaviour" ? (
                      <MutationStatus
                        pending={saveMutation.isPending}
                        error={saveMutation.error}
                        success="Saved"
                        pendingLabel="Saving…"
                      />
                    ) : null
                  }
                />
              )}

              {tab === "outcomes" && (
                <OutcomesTab
                  fields={form.post_call_fields}
                  onChange={(next) => updateField("post_call_fields", next)}
                  onSave={() => saveTab({ post_call_fields: form.post_call_fields }, "outcomes")}
                  saving={saveMutation.isPending && lastAction === "outcomes"}
                  status={
                    lastAction === "outcomes" ? (
                      <MutationStatus
                        pending={saveMutation.isPending}
                        error={saveMutation.error}
                        success="Saved"
                        pendingLabel="Saving…"
                      />
                    ) : null
                  }
                />
              )}
            </SurfaceCard>
          </BuilderSettingsTabs>
        </div>

        {/* Org-wide, not per-assistant: the endpoint has no assistant filter, and the
            heading says so. */}
        <div className="mt-[18px]">
          <AssistantAnalyticsPanel />
        </div>

        <SimulatorDrawer
          open={simulatorOpen}
          onClose={() => setSimulatorOpen(false)}
          assistantId={selectedId}
          assistantName={selected?.name ?? ""}
        />
      </section>
    </div>
  );
}
