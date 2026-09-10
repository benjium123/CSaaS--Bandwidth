import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  KB_DOCUMENTS_KEY,
  createKbDocumentFromText,
  createKbDocumentFromUrl,
  deleteKbDocument,
  fetchKbDocuments,
  uploadKbDocument,
  type KbStatus,
} from "@/api/assistants";
import {
  Button,
  Card,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  Textarea,
  type PillTone,
} from "@/components/ui/primitives";

/**
 * Per-workspace knowledge library, mounted in two places: Settings → AI → Knowledge and
 * the assistant builder's Knowledge tab. Documents are shared by every assistant in the
 * workspace, not scoped to a single assistant.
 */

type AddWay = "text" | "url" | "file";

function statusPill(status: KbStatus): { label: string; tone: PillTone } {
  switch (status) {
    case "pending":
      return { label: "Getting ready", tone: "warning" };
    case "indexed":
      return { label: "Ready", tone: "success" };
    case "failed":
      return { label: "Could not read it", tone: "danger" };
    default:
      return { label: "Getting ready", tone: "neutral" };
  }
}

export function KnowledgeTab() {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const [way, setWay] = React.useState<AddWay>("text");
  const [title, setTitle] = React.useState("");
  const [text, setText] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);

  const [armedDeleteId, setArmedDeleteId] = React.useState<string | null>(null);
  const armTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  function cancelDelete() {
    setArmedDeleteId(null);
    if (armTimerRef.current) clearTimeout(armTimerRef.current);
    armTimerRef.current = null;
  }

  function armDelete(id: string) {
    cancelDelete();
    setArmedDeleteId(id);
    armTimerRef.current = setTimeout(() => setArmedDeleteId(null), 8000);
  }

  React.useEffect(() => {
    return () => {
      if (armTimerRef.current) clearTimeout(armTimerRef.current);
    };
  }, []);

  const documentsQuery = useQuery({
    queryKey: KB_DOCUMENTS_KEY,
    queryFn: () => fetchKbDocuments(api),
    // A file or a web page is read in the background, so poll only while something is
    // still getting ready - and stop the moment nothing is.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((doc) => doc.status === "pending") ? 4000 : false,
  });

  const addDocumentMutation = useMutation({
    mutationFn: async () => {
      if (way === "text") {
        return createKbDocumentFromText(api, {
          title: title.trim(),
          text: text.trim(),
        });
      }
      if (way === "url") {
        return createKbDocumentFromUrl(api, {
          title: title.trim(),
          url: url.trim(),
        });
      }
      if (!file) {
        throw new Error("Choose a file to upload.");
      }
      return uploadKbDocument(api, { title: title.trim(), file });
    },
    onSuccess: () => {
      setTitle("");
      setText("");
      setUrl("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      void queryClient.invalidateQueries({ queryKey: KB_DOCUMENTS_KEY });
    },
  });

  const removeDocumentMutation = useMutation({
    mutationFn: (id: string) => deleteKbDocument(api, id),
    onSuccess: () => {
      cancelDelete();
      void queryClient.invalidateQueries({ queryKey: KB_DOCUMENTS_KEY });
    },
  });

  const bodyReady =
    way === "text" ? text.trim() !== "" : way === "url" ? url.trim() !== "" : file !== null;
  const canSubmit = title.trim() !== "" && bodyReady && !addDocumentMutation.isPending;
  const documents = documentsQuery.data ?? [];

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    addDocumentMutation.mutate();
  }

  return (
    <div className="space-y-6">
      <Section
        title="Add to your knowledge"
        description="Your assistants can look up anything you put here while a call is happening."
      >
        <form onSubmit={handleSubmit} className="space-y-3">
          <Select
            aria-label="How to add"
            value={way}
            onChange={(event) => setWay(event.target.value as AddWay)}
          >
            <option value="text">Paste text</option>
            <option value="url">Add a web page</option>
            <option value="file">Upload a file</option>
          </Select>

          <div className="space-y-1">
            <label htmlFor="kb-title" className="block text-xs text-muted-foreground">
              Title
            </label>
            <Input
              id="kb-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              required
            />
          </div>

          {way === "text" && (
            <div className="space-y-1">
              <label htmlFor="kb-text" className="block text-xs text-muted-foreground">
                Text
              </label>
              <Textarea
                id="kb-text"
                rows={5}
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="Paste what your assistant should know…"
              />
            </div>
          )}

          {way === "url" && (
            <div className="space-y-1">
              <label htmlFor="kb-url" className="block text-xs text-muted-foreground">
                Web address
              </label>
              <Input
                id="kb-url"
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://…"
              />
            </div>
          )}

          {way === "file" && (
            <div className="space-y-1">
              <label htmlFor="kb-file" className="block text-xs text-muted-foreground">
                File
              </label>
              {/* The primitives have no file input; this is the one raw input in this file
                  for exactly that reason. */}
              <input
                id="kb-file"
                ref={fileInputRef}
                type="file"
                accept=".pdf,.docx,.txt,.md"
                aria-label="Choose a file"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                className="block text-sm text-muted-foreground file:mr-4 file:rounded-md file:border file:border-border file:bg-background file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-foreground hover:file:bg-muted"
              />
              <p className="text-xs text-muted-foreground">PDF, Word, text or Markdown.</p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={!canSubmit}>
              Add to knowledge
            </Button>
            {addDocumentMutation.status !== "idle" && (
              <MutationStatus
                pending={addDocumentMutation.isPending}
                error={addDocumentMutation.error}
                success="Added"
                pendingLabel="Adding…"
              />
            )}
          </div>
        </form>
      </Section>

      <Section title="Your knowledge">
        {documentsQuery.isLoading ? (
          <Spinner label="Loading knowledge" />
        ) : documentsQuery.isError ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>Your knowledge is unavailable.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void documentsQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : documents.length === 0 ? (
          <EmptyState
            title="Nothing here yet"
            description="Add a file, a web page or some text your assistant can look things up in."
          />
        ) : (
          <Card className="p-0">
            <ul aria-label="Knowledge documents" className="divide-y divide-border">
              {documents.map((doc) => {
                const pill = statusPill(doc.status);
                return (
                  <li key={doc.id} className="space-y-2 p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="space-y-1">
                        <p className="text-sm font-medium">{doc.title}</p>
                        <Pill tone={pill.tone}>{pill.label}</Pill>
                        {doc.status === "indexed" && (
                          <p className="text-xs text-muted-foreground">
                            {doc.chunk_count} {doc.chunk_count === 1 ? "section" : "sections"}
                          </p>
                        )}
                        {doc.status === "failed" && doc.detail && (
                          <p className="text-xs text-destructive">{doc.detail}</p>
                        )}
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        aria-label={
                          armedDeleteId === doc.id
                            ? `Confirm remove ${doc.title}`
                            : `Remove ${doc.title}`
                        }
                        disabled={removeDocumentMutation.isPending}
                        onClick={() => {
                          if (armedDeleteId === doc.id) {
                            removeDocumentMutation.mutate(doc.id);
                          } else {
                            armDelete(doc.id);
                          }
                        }}
                      >
                        {armedDeleteId === doc.id ? `Confirm remove ${doc.title}` : "Remove"}
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </Card>
        )}
        {removeDocumentMutation.status !== "idle" && (
          <MutationStatus
            pending={removeDocumentMutation.isPending}
            error={removeDocumentMutation.error}
            success="Removed"
            pendingLabel="Removing…"
          />
        )}
      </Section>
    </div>
  );
}
