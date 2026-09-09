import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  useAgentProfiles,
  useCreateAgentProfile,
  useCreateKbDocument,
  useDeleteAgentProfile,
  useDeleteKbDocument,
  useKbDocument,
  useKbDocuments,
  useSetDefaultAgentProfile,
  useUpdateAgentProfile,
  type AgentProfileFields,
  type AgentProfileOut,
} from "@/api/hooks";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/** Item 47: a two-step "click again to confirm" destructive-action pattern - the first
 * click arms a "Confirm...?" state (with a Cancel escape hatch), the second click
 * within CONFIRM_TIMEOUT_MS actually performs the action. Left alone, it silently
 * reverts to the plain button so a stale armed state doesn't linger and get triggered
 * by an unrelated later click. */
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

const EMPTY_FORM: AgentProfileFields = {
  name: "",
  system_prompt: "",
  greeting: "",
  voice_id: "",
  llm_provider: "",
  llm_model: "",
  voicemail_message: "",
  sms_enabled: false,
  sms_turn_ceiling: 10,
  sms_handoff_keywords: [],
  sms_max_reply_chars: 480,
};

function formFromProfile(p: AgentProfileOut): AgentProfileFields {
  return {
    name: p.name,
    system_prompt: p.system_prompt,
    greeting: p.greeting,
    voice_id: p.voice_id,
    llm_provider: p.llm_provider,
    llm_model: p.llm_model,
    voicemail_message: p.voicemail_message,
    sms_enabled: p.sms_enabled,
    sms_turn_ceiling: p.sms_turn_ceiling,
    sms_handoff_keywords: p.sms_handoff_keywords,
    sms_max_reply_chars: p.sms_max_reply_chars,
  };
}

export function AgentPage() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const { data: profiles, isLoading } = useAgentProfiles(api);
  const createProfile = useCreateAgentProfile(api);
  const updateProfile = useUpdateAgentProfile(api);
  const deleteProfile = useDeleteAgentProfile(api);
  const setDefault = useSetDefaultAgentProfile(api);
  const deleteConfirm = useConfirm();

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [form, setForm] = React.useState<AgentProfileFields>(EMPTY_FORM);
  const [error, setError] = React.useState<string | null>(null);
  // Item 19: true the moment the user changes anything, cleared on a real selection
  // switch or a successful save.
  const [dirty, setDirty] = React.useState(false);

  const selected = (profiles ?? []).find((p) => p.id === selectedId) ?? null;

  // Handoff keywords are a string[] on the wire; edited here as one comma-separated
  // field, kept as its own piece of state so a trailing ", " while typing isn't
  // immediately collapsed away by round-tripping through the array.
  const [keywordsInput, setKeywordsInput] = React.useState("");

  // Item 19: keyed on `selectedId` (a stable primitive), NOT on `selected` (a fresh
  // object reference on every background refetch of the profiles list) - a refetch
  // that leaves the same profile selected must never re-run this and clobber whatever
  // the user is mid-typing. A real switch (including to "New profile", selectedId ->
  // null) always resyncs and always wins over a stale dirty flag from the profile just
  // left.
  React.useEffect(() => {
    const next = selected ? formFromProfile(selected) : EMPTY_FORM;
    setForm(next);
    setKeywordsInput((next.sms_handoff_keywords ?? []).join(", "));
    setDirty(false);
    deleteConfirm.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  function startNew() {
    setSelectedId(null);
    setForm(EMPTY_FORM);
    setKeywordsInput((EMPTY_FORM.sms_handoff_keywords ?? []).join(", "));
    setError(null);
  }

  function field<K extends keyof AgentProfileFields>(key: K) {
    return (value: string) => {
      setDirty(true);
      setForm((f) => ({ ...f, [key]: value }));
    };
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (selectedId) {
        await updateProfile.mutateAsync({ id: selectedId, ...form });
        setDirty(false);
      } else {
        const created = await createProfile.mutateAsync(form);
        // Item 44: make sure the just-created profile is actually IN `profiles` before
        // selecting it - otherwise `selected` resolves to null for a beat (the create
        // mutation's own onSuccess invalidation may not have finished refetching yet)
        // and the form flashes back to EMPTY_FORM instead of showing what was just made.
        await queryClient.invalidateQueries({ queryKey: ["agent-profiles"], refetchType: "active" });
        setSelectedId(created.id);
      }
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function remove(id: string) {
    setError(null);
    try {
      await deleteProfile.mutateAsync(id);
      if (selectedId === id) startNew();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function makeDefault(id: string) {
    setError(null);
    try {
      await setDefault.mutateAsync(id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const saving = createProfile.isPending || updateProfile.isPending;

  return (
    <div className="grid h-full grid-cols-[280px_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="flex items-center justify-between gap-2 border-b border-border p-3">
          <h1 className="text-lg font-semibold">AI Agent</h1>
          <Button type="button" size="sm" onClick={startNew}>
            New
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading profiles" />
          ) : (profiles ?? []).length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No agent profiles yet.</p>
          ) : (
            <ul aria-label="Agent profiles">
              {(profiles ?? []).map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    aria-current={p.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(p.id)}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-muted",
                      p.id === selectedId && "bg-muted",
                    )}
                  >
                    <span>{p.name}</span>
                    {p.is_default && (
                      <Badge className="bg-green-100 text-green-800">Default</Badge>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto p-6">
        <form className="max-w-xl space-y-4" onSubmit={save}>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            {selectedId ? "Edit profile" : "New profile"}
            {dirty && (
              <span className="text-[11px] font-normal text-muted-foreground">
                Unsaved changes
              </span>
            )}
          </h2>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="agent-name">
              Name
            </label>
            <Input
              id="agent-name"
              aria-label="Profile name"
              value={form.name}
              onChange={(e) => field("name")(e.target.value)}
              required
            />
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="agent-prompt">
              System prompt
            </label>
            <textarea
              id="agent-prompt"
              aria-label="System prompt"
              rows={6}
              className="flex w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
              value={form.system_prompt}
              onChange={(e) => field("system_prompt")(e.target.value)}
            />
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="agent-greeting">
              Greeting
            </label>
            <Input
              id="agent-greeting"
              aria-label="Greeting"
              value={form.greeting}
              onChange={(e) => field("greeting")(e.target.value)}
            />
          </div>

          <div className="grid grid-cols-3 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="agent-voice">
                Voice ID
              </label>
              <Input
                id="agent-voice"
                aria-label="Voice ID"
                value={form.voice_id}
                onChange={(e) => field("voice_id")(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="agent-llm-provider">
                LLM provider
              </label>
              <Input
                id="agent-llm-provider"
                aria-label="LLM provider"
                value={form.llm_provider}
                onChange={(e) => field("llm_provider")(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="agent-llm-model">
                LLM model
              </label>
              <Input
                id="agent-llm-model"
                aria-label="LLM model"
                value={form.llm_model}
                onChange={(e) => field("llm_model")(e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="agent-voicemail-message">
              Voicemail message
            </label>
            <textarea
              id="agent-voicemail-message"
              aria-label="Voicemail message"
              rows={3}
              placeholder="Spoken after the beep on outbound calls that hit voicemail. Leave empty to skip the drop."
              className="flex w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
              value={form.voicemail_message ?? ""}
              onChange={(e) => field("voicemail_message")(e.target.value)}
            />
          </div>

          <fieldset className="space-y-3 rounded-md border border-border p-3">
            <legend className="px-1 text-xs font-medium text-muted-foreground">
              SMS agent
            </legend>

            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.sms_enabled ?? false}
                onChange={(e) => {
                  setDirty(true);
                  setForm((f) => ({ ...f, sms_enabled: e.target.checked }));
                }}
              />
              Reply to inbound SMS automatically
            </label>

            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1">
                <label className="block text-xs text-muted-foreground" htmlFor="agent-sms-turn-ceiling">
                  Turn ceiling
                </label>
                <Input
                  id="agent-sms-turn-ceiling"
                  aria-label="Turn ceiling"
                  type="number"
                  min={1}
                  value={form.sms_turn_ceiling ?? 10}
                  onChange={(e) => {
                    setDirty(true);
                    setForm((f) => ({ ...f, sms_turn_ceiling: Number(e.target.value) }));
                  }}
                />
              </div>
              <div className="space-y-1">
                <label className="block text-xs text-muted-foreground" htmlFor="agent-sms-max-reply-chars">
                  Max reply chars
                </label>
                <Input
                  id="agent-sms-max-reply-chars"
                  aria-label="Max reply chars"
                  type="number"
                  min={1}
                  value={form.sms_max_reply_chars ?? 480}
                  onChange={(e) => {
                    setDirty(true);
                    setForm((f) => ({ ...f, sms_max_reply_chars: Number(e.target.value) }));
                  }}
                />
              </div>
            </div>

            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="agent-sms-handoff-keywords">
                Handoff keywords
              </label>
              <Input
                id="agent-sms-handoff-keywords"
                aria-label="Handoff keywords"
                placeholder="human, agent, representative"
                value={keywordsInput}
                onChange={(e) => {
                  const text = e.target.value;
                  setDirty(true);
                  setKeywordsInput(text);
                  setForm((f) => ({
                    ...f,
                    sms_handoff_keywords: text
                      .split(",")
                      .map((k) => k.trim())
                      .filter(Boolean),
                  }));
                }}
              />
              <p className="text-[11px] text-muted-foreground">Comma-separated.</p>
            </div>
          </fieldset>

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={!form.name.trim() || saving}>
              {selectedId ? "Save" : "Create"}
            </Button>
            {selected && (
              <>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => makeDefault(selected.id)}
                  disabled={selected.is_default || setDefault.isPending}
                >
                  {selected.is_default ? "Default" : "Make default"}
                </Button>
                {deleteConfirm.confirming ? (
                  <>
                    <Button
                      type="button"
                      variant="destructive"
                      onClick={() => {
                        deleteConfirm.cancel();
                        void remove(selected.id);
                      }}
                      disabled={deleteProfile.isPending}
                    >
                      Confirm delete?
                    </Button>
                    <Button type="button" variant="outline" onClick={deleteConfirm.cancel}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <Button
                    type="button"
                    variant="destructive"
                    onClick={deleteConfirm.requestConfirm}
                    disabled={deleteProfile.isPending}
                  >
                    Delete
                  </Button>
                )}
              </>
            )}
          </div>
        </form>

        <KbSection />
      </section>
    </div>
  );
}

/** Paste-text knowledge base editor (P9): list documents, create by pasting a title +
 * body (server chunks it), expand to view chunks, delete. */
function KbSection() {
  const { api } = useAuth();
  const { data: documents, isLoading } = useKbDocuments(api);
  const createDoc = useCreateKbDocument(api);
  const deleteDoc = useDeleteKbDocument(api);

  const [title, setTitle] = React.useState("");
  const [text, setText] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const { data: detail } = useKbDocument(api, expandedId);
  // Item 47/48: same two-step confirm as the agent profile Delete button, but keyed per
  // document since this is a list - only one row at a time may be "armed".
  const [confirmDeleteId, setConfirmDeleteId] = React.useState<string | null>(null);
  const confirmTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  function armDelete(id: string) {
    setConfirmDeleteId(id);
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = setTimeout(() => setConfirmDeleteId(null), CONFIRM_TIMEOUT_MS);
  }

  function cancelDelete() {
    setConfirmDeleteId(null);
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
  }

  React.useEffect(() => {
    return () => {
      if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    };
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createDoc.mutateAsync({ title, text });
      setTitle("");
      setText("");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function remove(id: string) {
    setError(null);
    try {
      await deleteDoc.mutateAsync(id);
      if (expandedId === id) setExpandedId(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="mt-8 max-w-xl space-y-4 border-t border-border pt-6">
      <h2 className="text-base font-semibold">Knowledge base</h2>
      <p className="text-xs text-muted-foreground">
        Text the AI agent can search mid-call. Pasted text is split into chunks
        automatically.
      </p>

      <form className="space-y-2" onSubmit={create}>
        <Input
          aria-label="Document title"
          placeholder="Title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
        />
        <textarea
          aria-label="Document text"
          rows={4}
          placeholder="Paste the text to index…"
          className="flex w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
          value={text}
          onChange={(e) => setText(e.target.value)}
          required
        />
        <Button type="submit" size="sm" disabled={!title.trim() || !text.trim() || createDoc.isPending}>
          Add document
        </Button>
      </form>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {isLoading ? (
        <Spinner label="Loading knowledge base" />
      ) : (documents ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No documents yet.</p>
      ) : (
        <ul aria-label="Knowledge base documents" className="divide-y divide-border rounded-md border border-border">
          {(documents ?? []).map((doc) => (
            <li key={doc.id} className="p-3">
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  className="text-left text-sm font-medium hover:underline"
                  onClick={() => setExpandedId((prev) => (prev === doc.id ? null : doc.id))}
                >
                  {doc.title}
                </button>
                {confirmDeleteId === doc.id ? (
                  <div className="flex shrink-0 gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="destructive"
                      onClick={() => {
                        cancelDelete();
                        void remove(doc.id);
                      }}
                      disabled={deleteDoc.isPending}
                    >
                      Confirm delete?
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={cancelDelete}>
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <Button
                    type="button"
                    size="sm"
                    variant="destructive"
                    onClick={() => armDelete(doc.id)}
                    disabled={deleteDoc.isPending}
                  >
                    Delete
                  </Button>
                )}
              </div>
              {expandedId === doc.id && detail && detail.id === doc.id && (
                <ul className="mt-2 space-y-2">
                  {detail.chunks.map((chunk) => (
                    <li
                      key={chunk.seq}
                      className="rounded-md bg-muted p-2 text-xs text-muted-foreground"
                    >
                      {chunk.text}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
