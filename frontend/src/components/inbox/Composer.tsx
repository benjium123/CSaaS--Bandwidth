import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { estimateSmsSegments } from "@/lib/format";
import { Button } from "@/components/ui/primitives";
import { useAuth } from "@/auth/AuthContext";
import { fetchOrgMembers, type OrgMember } from "@/api/conversations";
import { fetchTemplates, postThreadNote, type MessageTemplate } from "@/api/inboxPro";
import { cn } from "@/lib/utils";

/**
 * Compose + send, and P26 private notes + saved replies.
 *
 * The reply path deliberately keeps the original `onSend` contract: sending, Enter,
 * Shift+Enter, IME guard, the sticky_sender_unavailable reassign prompt and the segment
 * counter must not change. Note mode is a separate mutation, never a fallback inside
 * the reply path.
 */

function mentionTokenAt(
  value: string,
  caretIndex: number,
): { query: string; start: number; end: number } | null {
  const before = value.slice(0, caretIndex);
  const lastAt = before.lastIndexOf("@");
  if (lastAt < 0) return null;
  if (lastAt > 0 && !/\s/.test(before[lastAt - 1])) return null;
  const query = before.slice(lastAt + 1);
  if (/\s/.test(query)) return null;
  return { query, start: lastAt, end: caretIndex };
}

function mentionUserIdsForBody(body: string, mentions: Map<string, string>): string[] {
  const found = new Set<string>();
  for (const [label, userId] of mentions) {
    let searchFrom = 0;
    while (searchFrom <= body.length) {
      const index = body.indexOf(label, searchFrom);
      if (index === -1) break;
      const after = index + label.length;
      const nextChar = body[after];
      if (nextChar === undefined || !/[A-Za-z0-9'\-]/.test(nextChar)) {
        found.add(userId);
        break;
      }
      searchFrom = index + label.length;
    }
  }
  return Array.from(found);
}

export function Composer({
  onSend,
  disabled,
  threadId,
  onNoted,
}: {
  onSend: (body: string, allowReassign: boolean) => Promise<void>;
  disabled?: boolean;
  threadId?: string | null;
  onNoted?: () => void;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [mode, setMode] = React.useState<"reply" | "note">("reply");
  const [body, setBody] = React.useState("");
  const [caretIndex, setCaretIndex] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [needsReassign, setNeedsReassign] = React.useState(false);
  const reassignButtonRef = React.useRef<HTMLButtonElement>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const pendingCaret = React.useRef<number | null>(null);

  const [mentionSuppressed, setMentionSuppressed] = React.useState(false);
  const [mentionHighlight, setMentionHighlight] = React.useState(0);
  const [templateSuppressed, setTemplateSuppressed] = React.useState(false);
  const [templateHighlight, setTemplateHighlight] = React.useState(0);
  const [debouncedTemplateSearch, setDebouncedTemplateSearch] = React.useState("");

  const mentionIdsByLabel = React.useRef<Map<string, string>>(new Map());
  const noteTabDisabled = !threadId;

  const segments = React.useMemo(() => estimateSmsSegments(body), [body]);

  React.useEffect(() => {
    if (needsReassign) reassignButtonRef.current?.focus();
  }, [needsReassign]);

  React.useEffect(() => {
    if (mode === "note" && noteTabDisabled) setMode("reply");
  }, [mode, noteTabDisabled]);

  React.useEffect(() => {
    if (pendingCaret.current !== null && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.setSelectionRange(pendingCaret.current, pendingCaret.current);
      pendingCaret.current = null;
    }
  }, [body]);

  const mentionToken = React.useMemo(() => {
    if (mode !== "note") return null;
    return mentionTokenAt(body, caretIndex);
  }, [body, caretIndex, mode]);

  React.useEffect(() => {
    if (mentionToken) setMentionSuppressed(false);
  }, [mentionToken?.query, mentionToken?.start]);

  const quickPickOpen = mode === "reply" && body.startsWith("/") && !body.includes("\n");
  const quickPickSearch = quickPickOpen ? body.slice(1) : "";

  React.useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedTemplateSearch(quickPickSearch), 200);
    return () => window.clearTimeout(timer);
  }, [quickPickSearch]);

  const orgMembers = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
    enabled: mode === "note",
    staleTime: 60000,
  });

  const templatesQuery = useQuery({
    queryKey: ["templates", debouncedTemplateSearch],
    queryFn: () => fetchTemplates(api, debouncedTemplateSearch),
    enabled: quickPickOpen,
  });

  const members = orgMembers.data ?? [];
  const mentionMatches = React.useMemo(() => {
    if (!mentionToken || mentionSuppressed) return [];
    const query = mentionToken.query.toLowerCase();
    return members
      .filter((member) => member.full_name.toLowerCase().includes(query))
      .slice(0, 6);
  }, [members, mentionToken, mentionSuppressed]);

  const templates = templatesQuery.data ?? [];
  const mentionListVisible = mode === "note" && mentionToken !== null && !mentionSuppressed;
  const templateListVisible = quickPickOpen && !templateSuppressed;

  React.useEffect(() => {
    setMentionHighlight(0);
  }, [mentionToken?.query]);

  React.useEffect(() => {
    setTemplateHighlight(0);
  }, [debouncedTemplateSearch]);

  function selectMode(next: "reply" | "note") {
    setMode(next);
    setError(null);
    setNeedsReassign(false);
  }

  function focusTextareaCaret(index: number) {
    pendingCaret.current = index;
  }

  function handleBodyChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const next = e.currentTarget.value;
    setBody(next);
    setCaretIndex(e.currentTarget.selectionStart ?? next.length);
    if (mode === "reply" && next.startsWith("/") && !next.includes("\n")) {
      setTemplateSuppressed(false);
    }
  }

  function syncCaret(e: React.SyntheticEvent<HTMLTextAreaElement>) {
    setCaretIndex(e.currentTarget.selectionStart ?? e.currentTarget.value.length);
  }

  function pickMention(member: OrgMember) {
    if (!mentionToken) return;
    const label = `@${member.full_name}`;
    const inserted = `${label} `;
    const caret = mentionToken.start + inserted.length;
    mentionIdsByLabel.current.set(label, member.user_id);

    setBody(`${body.slice(0, mentionToken.start)}${inserted}${body.slice(mentionToken.end)}`);
    setCaretIndex(caret);
    focusTextareaCaret(caret);
    setMentionSuppressed(true);
    setMentionHighlight(0);
  }

  function pickTemplate(template: MessageTemplate) {
    setBody(template.body);
    setCaretIndex(template.body.length);
    focusTextareaCaret(template.body.length);
    setTemplateSuppressed(true);
    setTemplateHighlight(0);
  }

  async function submit(allowReassign: boolean) {
    if (!body.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSend(body.trim(), allowReassign);
      setBody("");
      setNeedsReassign(false);
    } catch (err) {
      if (err instanceof ApiError && err.code === "sticky_sender_unavailable") {
        setNeedsReassign(true);
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  const noteMutation = useMutation({
    mutationFn: async () => {
      if (!threadId) throw new Error("This conversation has no messages yet.");
      const finalBody = body.trim();
      return postThreadNote(api, threadId, finalBody, mentionUserIdsForBody(finalBody, mentionIdsByLabel.current));
    },
    onSuccess: () => {
      setBody("");
      setCaretIndex(0);
      setMode("reply");
      setMentionSuppressed(true);
      setTemplateSuppressed(false);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["timeline"] });
      onNoted?.();
    },
    onError: (err) => setError((err as Error).message),
  });

  function submitNote() {
    if (!body.trim()) return;
    if (!threadId) {
      setError("This conversation has no messages yet.");
      return;
    }
    setError(null);
    noteMutation.mutate();
  }

  function handleTextareaKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (mode === "note" && mentionListVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (mentionMatches.length > 0) {
          setMentionHighlight((index) => (index + 1) % mentionMatches.length);
        }
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        if (mentionMatches.length > 0) {
          setMentionHighlight((index) => (index - 1 + mentionMatches.length) % mentionMatches.length);
        }
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (mentionMatches.length > 0) pickMention(mentionMatches[mentionHighlight]);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        if (mentionMatches.length > 0) pickMention(mentionMatches[mentionHighlight]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMentionSuppressed(true);
        return;
      }
    }

    if (mode === "reply" && templateListVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (templates.length > 0) {
          setTemplateHighlight((index) => (index + 1) % templates.length);
        }
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        if (templates.length > 0) {
          setTemplateHighlight((index) => (index - 1 + templates.length) % templates.length);
        }
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (templates.length > 0) pickTemplate(templates[templateHighlight]);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        if (templates.length > 0) pickTemplate(templates[templateHighlight]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setTemplateSuppressed(true);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (mode === "note") {
        submitNote();
      } else {
        void submit(false);
      }
    }
  }

  return (
    <div
      className={cn(
        "space-y-2 p-3",
        mode === "note"
          ? "rounded-md border border-amber-500/40 bg-amber-500/10"
          : "border-t border-border",
      )}
    >
      <div role="tablist" aria-label="Message type" className="flex gap-2">
        <Button
          type="button"
          role="tab"
          aria-selected={mode === "reply"}
          onClick={() => selectMode("reply")}
          variant={mode === "reply" ? "default" : "ghost"}
          size="sm"
        >
          Reply
        </Button>
        <Button
          type="button"
          role="tab"
          aria-selected={mode === "note"}
          // A read-only grantee cannot post a note either (the server wants inbox:send),
          // so the tab is off rather than opening onto a field that cannot be submitted.
          disabled={noteTabDisabled || disabled}
          title={
            noteTabDisabled
              ? "This conversation has no messages yet - there is nothing to note on"
              : disabled
                ? "Read-only inbox — you can view but not post notes"
                : undefined
          }
          onClick={() => selectMode("note")}
          variant={mode === "note" ? "default" : "ghost"}
          size="sm"
        >
          Note
        </Button>
      </div>

      {needsReassign && mode === "reply" && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted p-2 text-xs"
        >
          <span>This conversation&rsquo;s number was retired. Send from a new number?</span>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => setNeedsReassign(false)}>
              Cancel
            </Button>
            <Button ref={reassignButtonRef} size="sm" onClick={() => submit(true)}>
              Send anyway
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}

      {mode === "note" && (
        <p className="text-xs text-muted-foreground">
          Only your team can see this. It is never sent to the contact.
        </p>
      )}

      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (mode === "note") {
            submitNote();
          } else {
            void submit(false);
          }
        }}
      >
        <div className="flex-1 space-y-1">
          <textarea
            ref={textareaRef}
            aria-label={mode === "note" ? "Note" : "Message"}
            placeholder={
              mode === "note"
                ? "Write a private note. Type @ to mention a teammate."
                : "Type a message"
            }
            value={body}
            disabled={disabled || busy || noteMutation.isPending}
            rows={1}
            onChange={handleBodyChange}
            onSelect={syncCaret}
            onKeyUp={(e) => {
              if (e.key.startsWith("Arrow")) syncCaret(e);
            }}
            onKeyDown={handleTextareaKeyDown}
            className="flex max-h-40 min-h-9 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
          />

          {/* Reply mode only: a note is never sent anywhere, so a segment count under it
              would be answering a question nobody asked - and one that costs money in
              every other place it appears. */}
          {mode === "reply" && (
            <p className="text-[11px] text-muted-foreground">
              {segments.units} char{segments.units === 1 ? "" : "s"} · {segments.encoding} ·{" "}
              {segments.segments} segment{segments.segments === 1 ? "" : "s"}
            </p>
          )}

          {mode === "note" && mentionListVisible && (
            <div
              role="listbox"
              aria-label="Mention a teammate"
              className="space-y-1 rounded-md border border-border bg-background p-1"
            >
              {mentionMatches.length === 0 ? (
                <p className="px-2 py-1 text-xs text-muted-foreground">No teammates match</p>
              ) : (
                mentionMatches.map((member, index) => (
                  <Button
                    key={member.user_id}
                    type="button"
                    role="option"
                    aria-selected={index === mentionHighlight}
                    variant="ghost"
                    size="sm"
                    className="w-full justify-start"
                    onClick={() => pickMention(member)}
                    onMouseEnter={() => setMentionHighlight(index)}
                  >
                    {member.full_name}
                  </Button>
                ))
              )}
            </div>
          )}

          {mode === "reply" && templateListVisible && (
            <div
              role="listbox"
              aria-label="Insert a saved reply"
              className="space-y-1 rounded-md border border-border bg-background p-1"
            >
              {templatesQuery.isLoading ? (
                <p className="px-2 py-1 text-xs text-muted-foreground">Searching...</p>
              ) : templates.length === 0 ? (
                <p className="px-2 py-1 text-xs text-muted-foreground">
                  No saved replies match
                </p>
              ) : (
                templates.map((template, index) => (
                  <Button
                    key={template.id}
                    type="button"
                    role="option"
                    aria-selected={index === templateHighlight}
                    variant="ghost"
                    size="sm"
                    className="w-full flex-col items-start justify-start text-left"
                    onClick={() => pickTemplate(template)}
                    onMouseEnter={() => setTemplateHighlight(index)}
                  >
                    <span className="font-medium">{template.name}</span>
                    <span className="max-w-full truncate text-xs text-muted-foreground">
                      {template.body}
                    </span>
                    {template.tokens.length > 0 && (
                      <span className="text-xs text-muted-foreground">
                        Fills in: {template.tokens.join(", ")}
                      </span>
                    )}
                  </Button>
                ))
              )}
            </div>
          )}
        </div>

        <Button
          type="submit"
          disabled={disabled || busy || noteMutation.isPending || !body.trim()}
        >
          {mode === "note" ? "Post note" : "Send"}
        </Button>
      </form>
    </div>
  );
}
