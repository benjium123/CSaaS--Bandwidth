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
        className="space-y-4"
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
          <label htmlFor="assistant-name" className="block text-xs text-muted-foreground">
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
          <label htmlFor="assistant-greeting" className="block text-xs text-muted-foreground">
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
          <label htmlFor="assistant-language" className="block text-xs text-muted-foreground">
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
          <label htmlFor="assistant-voice" className="block text-xs text-muted-foreground">
            Voice id
          </label>
          <Input
            id="assistant-voice"
            aria-label="Voice id"
            value={form.voice_id}
            onChange={(event) => updateField("voice_id", event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            The voice id from your voice provider.
          </p>
        </div>

        {/* The sample is generated from the SAVED voice provider but the TYPED voice id, so
            trying a different voice does not need a save first. */}
        <VoicePreviewButton
          ttsProvider={selected?.tts_provider ?? ""}
          voiceId={form.voice_id}
        />

        <div className="flex flex-wrap items-center gap-3">
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
        className="space-y-4"
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
          <label htmlFor="assistant-instructions" className="block text-xs text-muted-foreground">
            Instructions
          </label>
          <Textarea
            id="assistant-instructions"
            rows={8}
            aria-label="Instructions"
            value={form.system_prompt}
            onChange={(event) => updateField("system_prompt", event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            How your assistant should behave.
          </p>
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-goals" className="block text-xs text-muted-foreground">
            Goals
          </label>
          <Textarea
            id="assistant-goals"
            rows={4}
            aria-label="Goals"
            value={form.goals}
            onChange={(event) => updateField("goals", event.target.value)}
          />
          <p className="text-xs text-muted-foreground">What a good call achieves.</p>
        </div>

        <div className="space-y-1">
          <label htmlFor="assistant-guardrails" className="block text-xs text-muted-foreground">
            Guardrails
          </label>
          <Textarea
            id="assistant-guardrails"
            rows={4}
            aria-label="Guardrails"
            value={form.guardrails}
            onChange={(event) => updateField("guardrails", event.target.value)}
          />
          <p className="text-xs text-muted-foreground">What it must never do.</p>
        </div>

        <Section title="What your assistant actually gets" className="space-y-3">
          <p className="text-xs text-muted-foreground">
            The first paragraph is always included and cannot be changed.
          </p>
          <p className="text-muted-foreground">{COMPLIANCE_PREAMBLE}</p>
          {/* The server's effective_prompt wins when present because the server owns the
              real merge of these fields. */}
          <Textarea
            readOnly
            aria-label="Full instructions"
            rows={10}
            value={selected?.effective_prompt ?? composeEffectivePrompt(form)}
          />
        </Section>

        <div className="flex flex-wrap items-center gap-3">
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
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="flex items-center justify-between gap-2 border-b border-border p-3">
          <h1 className="text-lg font-semibold">Assistants</h1>
          <Button type="button" size="sm" onClick={startNew}>
            New
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {assistantsQuery.isLoading ? (
            <Spinner label="Loading assistants" />
          ) : assistantsQuery.isError ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              Assistants are unavailable.
            </p>
          ) : (assistantsQuery.data ?? []).length === 0 ? (
            <EmptyState
              title="No assistants yet."
              description="Create one to answer your calls."
            />
          ) : (
            <ul aria-label="Assistants">
              {(assistantsQuery.data ?? []).map((assistant) => (
                <li key={assistant.id}>
                  <Button
                    type="button"
                    variant="ghost"
                    aria-current={assistant.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(assistant.id)}
                    className={`h-auto w-full justify-between px-3 py-2 text-left text-sm ${
                      assistant.id === selectedId ? "bg-muted" : ""
                    }`}
                  >
                    <span>{assistant.name}</span>
                    {assistant.is_default && <Pill tone="success">Default</Pill>}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <h1 className="text-lg font-semibold">
            {selected ? selected.name : "New assistant"}
          </h1>

          {selected && (
            <div className="flex flex-wrap items-center gap-2">
              {dirty && (
                <span className="text-[11px] text-muted-foreground">Unsaved changes</span>
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
            className="space-y-1 rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
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
        <div className="mt-4 rounded-md border border-border p-3">
          <CallMePanel assistantId={selectedId} />
        </div>

        <div className="mt-4">
          <BuilderSettingsTabs
            id="assistant-builder"
            tabs={tabs}
            value={tab}
            onChange={setTab}
            ariaLabel="Assistant settings"
          >
            <div className="max-w-2xl space-y-4">
              {!selectedId && (
                <p className="text-sm text-muted-foreground">
                  Save this assistant first, then you can set up the rest.
                </p>
              )}

              {tab === "persona" && renderPersona()}

              {tab === "instructions" && renderInstructions()}

              {tab === "knowledge" && (
                <>
                  <p className="text-sm text-muted-foreground">
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
            </div>
          </BuilderSettingsTabs>
        </div>

        {/* Org-wide, not per-assistant: the endpoint has no assistant filter, and the
            heading says so. */}
        <div className="mt-8">
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
