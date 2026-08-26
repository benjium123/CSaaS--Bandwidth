import * as React from "react";
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

const EMPTY_FORM: AgentProfileFields = {
  name: "",
  system_prompt: "",
  greeting: "",
  voice_id: "",
  llm_provider: "",
  llm_model: "",
  voicemail_message: "",
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
  };
}

export function AgentPage() {
  const { api } = useAuth();
  const { data: profiles, isLoading } = useAgentProfiles(api);
  const createProfile = useCreateAgentProfile(api);
  const updateProfile = useUpdateAgentProfile(api);
  const deleteProfile = useDeleteAgentProfile(api);
  const setDefault = useSetDefaultAgentProfile(api);

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [form, setForm] = React.useState<AgentProfileFields>(EMPTY_FORM);
  const [error, setError] = React.useState<string | null>(null);

  const selected = (profiles ?? []).find((p) => p.id === selectedId) ?? null;

  React.useEffect(() => {
    setForm(selected ? formFromProfile(selected) : EMPTY_FORM);
  }, [selected]);

  function startNew() {
    setSelectedId(null);
    setForm(EMPTY_FORM);
    setError(null);
  }

  function field<K extends keyof AgentProfileFields>(key: K) {
    return (value: string) => setForm((f) => ({ ...f, [key]: value }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (selectedId) {
        await updateProfile.mutateAsync({ id: selectedId, ...form });
      } else {
        const created = await createProfile.mutateAsync(form);
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
          <h2 className="text-base font-semibold">
            {selectedId ? "Edit profile" : "New profile"}
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
                <Button
                  type="button"
                  variant="destructive"
                  onClick={() => remove(selected.id)}
                  disabled={deleteProfile.isPending}
                >
                  Delete
                </Button>
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
                <Button
                  type="button"
                  size="sm"
                  variant="destructive"
                  onClick={() => remove(doc.id)}
                  disabled={deleteDoc.isPending}
                >
                  Delete
                </Button>
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
