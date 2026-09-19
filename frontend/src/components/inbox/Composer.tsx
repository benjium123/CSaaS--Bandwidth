import * as React from "react";
import { ArrowRight, Clock, MessageSquare, Paperclip, StickyNote } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { estimateSmsSegments } from "@/lib/format";
import { Button } from "@/components/ui/primitives";
import { useAuth } from "@/auth/AuthContext";
import { fetchOrgMembers, type OrgMember } from "@/api/conversations";
import { fetchTemplates, postThreadNote, type MessageTemplate } from "@/api/inboxPro";
import {
  ALLOWED_MEDIA_TYPES,
  attachmentLimitSentence,
  datetimeLocalMin,
  mediaRejectionReason,
  scheduleRejectionReason,
  toIsoWithOffset,
  trackableUrls,
  uploadMedia,
  type MediaAttachment,
} from "@/api/messaging";
import { cn } from "@/lib/utils";

/**
 * Compose + send, and P26 private notes + saved replies.
 *
 * The reply path deliberately keeps the original `onSend` contract: sending, Enter,
 * Shift+Enter, IME guard, the sticky_sender_unavailable reassign prompt and the segment
 * counter must not change. Note mode is a separate mutation, never a fallback inside
 * the reply path.
 */

type Attachment = { name: string; media: MediaAttachment };

export interface ComposerExtras {
  media_ids?: string[];
  scheduled_for?: string;
  track_links?: boolean;
}

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
  onSend: (body: string, allowReassign: boolean, extras?: ComposerExtras) => Promise<void>;
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

  const [attachments, setAttachments] = React.useState<Attachment[]>([]);
  const [uploading, setUploading] = React.useState(false);
  const [scheduleOpen, setScheduleOpen] = React.useState(false);
  const [scheduledLocal, setScheduledLocal] = React.useState("");
  const [trackClicks, setTrackClicks] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const mentionIdsByLabel = React.useRef<Map<string, string>>(new Map());
  const noteTabDisabled = !threadId;

  const segments = React.useMemo(() => estimateSmsSegments(body), [body]);
  const trackableUrlCount = React.useMemo(
    () => (mode === "reply" ? trackableUrls(body).length : 0),
    [mode, body],
  );

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

  React.useEffect(() => {
    if (trackableUrlCount === 0) setTrackClicks(false);
  }, [trackableUrlCount]);

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

  function handleFiles(e: React.ChangeEvent<HTMLInputElement>) {
    // React nulls `currentTarget` once the handler returns, so the element has to be
    // captured here - reading e.currentTarget inside the promise callbacks below would
    // throw rather than clear the input.
    const input = e.currentTarget;
    const files = Array.from(input.files ?? []);
    if (files.length === 0) return;

    setError(null);

    // Validate everything before any network call; a bad file should fail instantly, not
    // after some siblings have already uploaded.
    for (const file of files) {
      const rejection = mediaRejectionReason(file);
      if (rejection) {
        setError(rejection);
        input.value = "";
        return;
      }
    }

    setUploading(true);
    Promise.all(files.map((file) => uploadMedia(api, file)))
      .then((uploaded) => {
        setAttachments((prev) => [
          ...prev,
          ...uploaded.map((media, index) => ({ name: files[index].name, media })),
        ]);
      })
      .catch((err) => setError((err as Error).message))
      .finally(() => {
        setUploading(false);
        // Clearing the value is what makes re-picking the SAME file fire `change` again.
        input.value = "";
      });
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((attachment) => attachment.media.id !== id));
  }

  function buildComposerExtras(): ComposerExtras | undefined {
    const extras: ComposerExtras = {};
    if (attachments.length > 0) {
      extras.media_ids = attachments.map((attachment) => attachment.media.id);
    }
    if (scheduledLocal) {
      extras.scheduled_for = toIsoWithOffset(scheduledLocal);
    }
    if (trackClicks && trackableUrlCount > 0) {
      extras.track_links = true;
    }
    return Object.keys(extras).length > 0 ? extras : undefined;
  }

  async function submit(allowReassign: boolean) {
    if (!body.trim()) return;

    if (mode === "reply" && scheduledLocal) {
      const rejection = scheduleRejectionReason(scheduledLocal);
      if (rejection) {
        setError(rejection);
        return;
      }
    }

    setBusy(true);
    setError(null);
    try {
      const extras = mode === "reply" ? buildComposerExtras() : undefined;
      if (extras) {
        await onSend(body.trim(), allowReassign, extras);
      } else {
        await onSend(body.trim(), allowReassign);
      }
      setBody("");
      setNeedsReassign(false);
      if (mode === "reply") {
        setAttachments([]);
        setScheduledLocal("");
        setScheduleOpen(false);
        setTrackClicks(false);
      }
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
          // Note mode wears the reference's `.note` colouring: --cx-flag, yellow, not orange.
          ? "rounded-md border border-[hsl(var(--cx-flag)/0.4)] bg-[hsl(var(--cx-flag)/0.1)]"
          // The composer is the primary action on the primary screen and used to be a
          // hairline away from being part of the timeline. It now sits on its own plane.
          : "cx-composer",
      )}
    >
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

      {mode === "reply" && (
        <div className="space-y-2">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ALLOWED_MEDIA_TYPES.join(",")}
            // A DIFFERENT label from the button that clicks it: two controls sharing one
            // accessible name is ambiguous to a screen reader and to getByLabelText.
            aria-label="Choose files to attach"
            className="hidden"
            onChange={handleFiles}
          />

          {scheduleOpen && (
            <div className="space-y-2 rounded-md border border-border bg-background p-2">
              <label className="block text-xs text-muted-foreground">
                Send at
                <input
                  type="datetime-local"
                  aria-label="Send at"
                  min={datetimeLocalMin()}
                  value={scheduledLocal}
                  onChange={(e) => {
                    setScheduledLocal(e.currentTarget.value);
                    setError(null);
                  }}
                  className="mt-1 flex h-9 w-full rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2"
                />
              </label>
              {scheduledLocal && (
                <p className="text-xs text-muted-foreground">
                  Sending {new Date(scheduledLocal).toLocaleString()}
                </p>
              )}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setScheduledLocal("");
                  setError(null);
                }}
              >
                Clear
              </Button>
            </div>
          )}

          {trackableUrlCount > 0 && (
            <label className="flex items-start gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                // The label element wraps the hint too, so its text is not a stable
                // accessible name - name the control explicitly instead.
                aria-label="Track link clicks"
                checked={trackClicks}
                onChange={(e) => setTrackClicks(e.currentTarget.checked)}
                className="mt-0.5"
              />
              <span>
                <span className="text-foreground">Track link clicks</span>
                {/* The hint appears ONLY while the box is checked: the server rewrites a
                    link only when track_links is true, so showing it otherwise would
                    promise tracking that will not happen. */}
                {trackClicks && (
                  <span className="block">
                    We&rsquo;ll swap the link for a trackable one so you can see if it was
                    opened.
                  </span>
                )}
              </span>
            </label>
          )}

          {/* The limit is a constraint on the attach button, so it is stated where that
              button is - as its tooltip - and repeated in full ONLY once there is
              something attached to measure against it. A permanent line of small print
              under the composer is what the reference is free of. */}
          {attachments.length > 0 && (
            <p className="text-xs text-muted-foreground">{attachmentLimitSentence()}</p>
          )}

          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {attachments.map((attachment) => (
                <span
                  key={attachment.media.id}
                  className="inline-flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-1 text-xs"
                >
                  {attachment.name}
                  <Button
                    type="button"
                    aria-label={`Remove ${attachment.name}`}
                    variant="ghost"
                    size="sm"
                    className="h-4 w-4 p-0"
                    onClick={() => removeAttachment(attachment.media.id)}
                  >
                    ×
                  </Button>
                </span>
              ))}
            </div>
          )}

          {uploading && <p className="text-xs text-muted-foreground">Uploading…</p>}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (mode === "note") {
            submitNote();
          } else {
            void submit(false);
          }
        }}
      >
        {/* The reference's `.composer-box`: field on top, a row of small icon buttons and
            the send button underneath, all inside one box that takes the focus ring. */}
        <div className="cx-composer-box space-y-1 px-4 py-3">
          <textarea
            ref={textareaRef}
            aria-label={mode === "note" ? "Note" : "Message"}
            placeholder={
              mode === "note"
                ? "Write a private note. Type @ to mention a teammate."
                : "Write a message…"
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
            className="cx-input flex max-h-40 min-h-9 w-full resize-y px-0 py-1 text-sm placeholder:text-muted-foreground"
          />

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
          {/* The reference's `.composer-row`: small icon buttons, the segment count, and
              the send button at the far end. Every one of these was a text button or a
              text tab before; not one capability has left. */}
          <div className="flex items-center gap-1 pt-1">
            <div role="tablist" aria-label="Message type" className="flex items-center gap-1">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "reply"}
                aria-label="Reply"
                title="Reply"
                onClick={() => selectMode("reply")}
                className="cx-icon-btn grid h-8 w-8 place-items-center"
              >
                <MessageSquare className="h-4 w-4" aria-hidden="true" />
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "note"}
                aria-label="Internal note"
                // A read-only grantee cannot post a note either (the server wants
                // inbox:send), so the control is off rather than opening onto a field
                // that cannot be submitted.
                disabled={noteTabDisabled || disabled}
                title={
                  noteTabDisabled
                    ? "This conversation has no messages yet - there is nothing to note on"
                    : disabled
                      ? "Read-only inbox — you can view but not post notes"
                      : "Internal note"
                }
                onClick={() => selectMode("note")}
                className="cx-icon-btn grid h-8 w-8 place-items-center disabled:pointer-events-none disabled:opacity-40"
              >
                <StickyNote className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>

            {/* Attaching and scheduling apply to a message, never to a note - a note is
                not sent anywhere and has nothing to attach to. */}
            {mode === "reply" && (
              <>
                <button
                  type="button"
                  aria-label="Attach a file"
                  title={attachmentLimitSentence()}
                  disabled={disabled || busy || uploading}
                  onClick={() => fileInputRef.current?.click()}
                  className="cx-icon-btn grid h-8 w-8 place-items-center disabled:pointer-events-none disabled:opacity-40"
                >
                  <Paperclip className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  aria-label="Send later"
                  title="Send later"
                  aria-expanded={scheduleOpen}
                  disabled={disabled || busy || uploading}
                  onClick={() => setScheduleOpen((open) => !open)}
                  className="cx-icon-btn grid h-8 w-8 place-items-center disabled:pointer-events-none disabled:opacity-40"
                >
                  <Clock className="h-4 w-4" aria-hidden="true" />
                </button>
              </>
            )}

            {/* Reply mode only: a note is never sent anywhere, so a segment count under it
                would be answering a question nobody asked - and one that costs money in
                every other place it appears. */}
            {mode === "reply" && (
              <span className="cx-num ml-2 text-[0.625rem] text-muted-foreground">
                {segments.units} char{segments.units === 1 ? "" : "s"} · {segments.encoding} ·{" "}
                {segments.segments} segment{segments.segments === 1 ? "" : "s"}
              </span>
            )}

            <Button
              type="submit"
              className="cx-send ml-auto gap-2 rounded-full px-4"
              disabled={disabled || busy || noteMutation.isPending || uploading || !body.trim()}
            >
              {mode === "note" ? "Post note" : scheduledLocal ? "Schedule" : "Send"}
              {mode === "reply" && !scheduledLocal && (
                <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
