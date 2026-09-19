/**
 * The contact panel, cut back to what console-reference.html's `<aside class="panel">`
 * draws: a close X, a large avatar, the name, and exactly three `.f` rows — Phone, Email,
 * Company.
 *
 * What was removed, and where it still lives (verified, not assumed):
 *   Owner / Team  -> ContactsPage's table columns (Owner, Team) and AssignOwnerDrawer,
 *                    which is the only place either is EDITABLE anyway.
 *   "Open contact" -> the avatar is now the link to /contacts/:contactId, so the route
 *                    through to the full record survives without a fifth thing on screen.
 *   Call / Message buttons -> the Phone row's PhoneNumberMenu already offers Text and
 *                    Call for this same number, so the pair of icon buttons was a
 *                    duplicate control, not a capability.
 *   "Shared with"  -> InboxSettingsPage's InboxGrantEditor, which can also EDIT grants.
 *
 * What was NOT removed, because this panel is the ONLY place in the app that can reach
 * it: Role, Address, contact Notes and the tag list. They are folded into the two
 * disclosures at the bottom. Deleting them would delete the feature, not relocate it.
 */
import * as React from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import {
  addContactNote,
  fetchContact,
  fetchContactNotes,
  updateContact,
  updateContactAttributes,
  type Contact,
  type Conversation,
  type Inbox,
} from "@/api/conversations";
import { Button, Collapsible, Input, Pill } from "@/components/ui/primitives";
import { PhoneNumberMenu } from "@/components/ui/PhoneNumberMenu";
import { avatarHueIndex, formatPhone, initialsOf, relativeTime } from "@/lib/format";
import { avatarSeedFor } from "./ConversationList";
import { cn } from "@/lib/utils";

/** The reference's `.f`: key on the left, value on the right, a hairline above. */
function PanelField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="cx-field">
      <span className="cx-field-k">{label}</span>
      <span className="cx-field-v min-w-0">{children}</span>
    </div>
  );
}

/** attributes[key] is arbitrary JSON (backend custom fields support number/select kinds
 * too) - these four are always edited as plain text here, so coerce defensively rather
 * than assume every stored value is already a string. */
function attrText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : String(value);
}

type SaveStatus = "idle" | "saving" | "saved" | "error";

/** Inline edit with a visible saving/saved/error state (plan follow-up: company, role,
 * email, address must never silently no-op). On failure the field stays open with the
 * error shown and the user's typed value intact - it never pretends the save happened. */
function EditableField({
  label,
  value,
  onSave,
  type = "text",
  hideLabel = false,
  align = "start",
  displayClassName,
  emptyText = "Add",
}: {
  label: string;
  value: string;
  onSave: (value: string) => Promise<void>;
  type?: string;
  /** Set when the field sits inside a `PanelField`, which already prints the label in its
   * own key column - printing it twice would read as two different fields. The label is
   * still the control's accessible name; only the visible heading goes. */
  hideLabel?: boolean;
  align?: "start" | "center" | "end";
  displayClassName?: string;
  /** What the resting control shows when there is no value yet. The panel's three lead
   * rows pass an em-dash so the row keeps its shape instead of advertising the "Add"
   * link the approved design does not have. */
  emptyText?: React.ReactNode;
}) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(value);
  const [status, setStatus] = React.useState<SaveStatus>("idle");
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setDraft(value);
  }, [value]);

  // A "saved" confirmation is transient - clear it back to idle after a beat so it
  // doesn't linger forever once the field is collapsed again.
  React.useEffect(() => {
    if (status !== "saved") return undefined;
    const timer = setTimeout(() => setStatus("idle"), 2000);
    return () => clearTimeout(timer);
  }, [status]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setStatus("saving");
    setError(null);
    try {
      await onSave(draft);
      setStatus("saved");
      setEditing(false);
    } catch (err) {
      // Stay in edit mode: the failure and the user's draft both remain visible instead
      // of silently reverting to the last-saved value.
      setStatus("error");
      setError(err instanceof Error ? err.message : "Failed to save");
    }
  }

  const alignClass =
    align === "center"
      ? "justify-center"
      : align === "end"
        ? "justify-end"
        : "justify-start";

  return (
    <div className="space-y-1">
      <div className={cn("flex items-center gap-2", alignClass)}>
        {!hideLabel && <p className="cx-label">{label}</p>}
        {status === "saving" && (
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> Saving…
          </span>
        )}
        {status === "saved" && (
          <span className="text-[10px] text-[hsl(var(--cx-live))]">Saved</span>
        )}
      </div>
      {editing ? (
        <form className="flex items-center gap-1" onSubmit={handleSubmit}>
          <Input
            aria-label={label}
            type={type}
            value={draft}
            disabled={status === "saving"}
            onChange={(e) => setDraft(e.target.value)}
            className="h-8 text-xs"
          />
          <Button
            type="submit"
            variant="ghost"
            size="icon"
            disabled={status === "saving"}
            aria-label={`Save ${label}`}
            className="h-7 w-7 shrink-0 text-foreground hover:bg-muted disabled:opacity-50"
          >
            <Check className="h-3.5 w-3.5" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => {
              setDraft(value);
              setStatus("idle");
              setError(null);
              setEditing(false);
            }}
            aria-label={`Cancel ${label}`}
            className="h-7 w-7 shrink-0 text-foreground hover:bg-muted"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </form>
      ) : (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setEditing(true)}
          aria-label={`Edit ${label}`}
          className={cn(
            "w-full rounded-md px-2 py-1 text-xs text-foreground hover:bg-muted",
            alignClass,
            align === "end" && "text-right",
            align === "center" && "text-center",
            displayClassName,
          )}
        >
          {value || emptyText}
        </Button>
      )}
      {status === "error" && error && (
        <p role="alert" className="text-[11px] text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** Email and Company can both be blank. The row is still drawn, with a muted em-dash, so
 * the panel is the same three rows tall for every contact rather than growing and
 * shrinking as records fill in. */
function EmptyValue() {
  return <span className="text-muted-foreground">—</span>;
}

export function ContactPanel({
  conversation,
  canSend = true,
  onClose,
  className,
}: {
  conversation: Conversation | null;
  /** Retained for callers; the panel no longer renders anything inbox-scoped (inbox
   * sharing moved to InboxSettingsPage, which can also edit it). */
  inbox: Inbox | null;
  /** F2: viewers (my_role "viewer") can see the conversation but not act on it. */
  canSend?: boolean;
  /** The reference's `.panel-close`: the panel is opened on request and must be
   * dismissable from inside itself, not only from the header button that opened it. Omit
   * to render no X (a caller that pins the panel open). */
  onClose?: () => void;
  className?: string;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const contactId = conversation?.contact?.id ?? null;
  const contactQuery = useQuery({
    queryKey: ["contact", contactId],
    queryFn: () => fetchContact(api, contactId as string),
    enabled: Boolean(contactId),
  });

  const attributes = contactQuery.data?.attributes ?? {};

  const [name, setName] = React.useState(
    contactQuery.data?.display_name ?? conversation?.contact?.display_name ?? "",
  );
  const [company, setCompany] = React.useState(attrText(attributes.company));
  const [role, setRole] = React.useState(attrText(attributes.role));
  const [email, setEmail] = React.useState(attrText(attributes.email));
  const [address, setAddress] = React.useState(attrText(attributes.address));
  const [newNote, setNewNote] = React.useState("");

  React.useEffect(() => {
    const attrs = contactQuery.data?.attributes ?? {};
    setName(contactQuery.data?.display_name ?? conversation?.contact?.display_name ?? "");
    setCompany(attrText(attrs.company));
    setRole(attrText(attrs.role));
    setEmail(attrText(attrs.email));
    setAddress(attrText(attrs.address));
  }, [contactQuery.data, conversation]);

  // Item 3: notes are a proper list via GET/POST /api/v1/contacts/{id}/notes - not a
  // single free-text field on the contact (the old PATCH silently no-op'd; the backend
  // schema never accepted a `notes` key).
  const notesQuery = useQuery({
    queryKey: ["contact-notes", contactId],
    queryFn: () => fetchContactNotes(api, contactId as string),
    enabled: Boolean(contactId),
  });

  // Declared before the early return below - conditionally skipping a hook call would
  // violate the Rules of Hooks the moment `conversation` toggles between null and set.
  const addNoteMutation = useMutation({
    mutationFn: (body: string) => {
      if (!contactId) throw new Error("No contact to save notes for");
      return addContactNote(api, contactId, body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["contact-notes", contactId] });
      setNewNote("");
    },
  });

  if (!conversation) {
    return (
      // Quiet, not silent: the panel keeps its place so the grid does not reflow when a
      // conversation is chosen, but it no longer competes with the timeline's empty state
      // for the same sentence.
      <div
        className={cn("cx-panel flex h-full items-center justify-center px-3", className)}
      >
        <span className="cx-empty text-xs">No contact selected</span>
      </div>
    );
  }

  const title =
    conversation.contact?.display_name ?? formatPhone(conversation.contact_e164);

  async function saveField(patch: Partial<Contact>) {
    if (!contactId) return;
    await updateContact(api, contactId, patch);
    await queryClient.invalidateQueries({ queryKey: ["contact", contactId] });
  }

  /** Merges into the LATEST known attributes (not a stale closure) before sending, since
   * the backend PATCH replaces `attributes` wholesale rather than merging server-side. */
  async function saveAttribute(key: string, value: string) {
    if (!contactId) return;
    const latest = queryClient.getQueryData<Contact>(["contact", contactId]);
    await updateContactAttributes(api, contactId, latest?.attributes ?? attributes, {
      [key]: value,
    });
    await queryClient.invalidateQueries({ queryKey: ["contact", contactId] });
  }

  function focusComposer() {
    document.querySelector<HTMLInputElement>('input[aria-label="Message"]')?.focus();
  }

  // There is NO endpoint that returns a contact's tags today (backend has PUT
  // /contacts/{id}/tags and GET /tags, but ContactOut carries no tags and there is no
  // GET /contacts/{id}/tags), so do NOT fetch anything and do not render an empty Tags
  // section.
  const tags = Array.isArray(attributes.tags)
    ? (attributes.tags as string[]).filter((tag): tag is string => typeof tag === "string")
    : [];
  const tagsToRender = tags.length > 0 ? tags : null;

  const otherPhones = (contactQuery.data?.phones ?? []).filter(
    (phone) => phone.e164 !== conversation.contact_e164,
  );

  // The reference's `.av.xl`, in the same hue this contact wears in the list and in the
  // thread header - one person, one colour, everywhere.
  const avatar = (
    <span
      data-hue={avatarHueIndex(avatarSeedFor(conversation))}
      aria-hidden="true"
      className="cx-avatar cx-panel-av flex items-center justify-center rounded-full font-semibold"
    >
      {initialsOf(title)}
    </span>
  );

  return (
    <aside
      className={cn(
        // `relative` is what the close button positions against - without it the X would
        // anchor to the page and float over whatever is scrolled under it.
        "cx-panel relative h-full overflow-y-auto border-l border-border p-[22px] text-foreground",
        className,
      )}
      aria-label="Contact panel"
    >
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          aria-label="Close contact panel"
          title="Close"
          className="cx-icon-btn absolute right-3 top-3 grid h-8 w-8 place-items-center"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      )}

      {/* The reference's `.panel-head`: the face, then the name. Nothing else. */}
      <div className="cx-panel-head">
        {/* The single route through to the full record. The reference draws a plain
            block, but a customer must still be able to reach Owner, Team, duplicates,
            export and erase - the avatar carries it so the head stays two things. */}
        {contactId ? (
          <Link
            to={`/contacts/${contactId}`}
            // P22 rule: this link can 404 when the contact-visibility policy excludes the
            // user. That is correct - the panel still shows the conversation, but the
            // contact record itself is not visible to them.
            aria-label={`Open contact record for ${title}`}
            className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--cx-accent))]"
          >
            {avatar}
          </Link>
        ) : (
          avatar
        )}

        {/* No "NAME" label above it: the name IS the heading. Clicking it renames the
            contact, which this panel is the only place in the app that can do. */}
        {contactId ? (
          <EditableField
            label="Name"
            hideLabel
            align="center"
            emptyText="Unnamed"
            // The size/weight are repeated as utilities, not left to `.cx-panel-name`
            // alone: `cn` is tailwind-merge, so these DELETE the Button's own `text-xs`
            // and `font-medium`. Relying on the stylesheet would leave the winner to the
            // import order of index.css vs consoleTheme.css, which is not a thing to bet
            // the panel's heading on.
            displayClassName="cx-panel-name h-auto text-[1.25rem] font-semibold"
            value={name}
            onSave={async (value) => {
              await saveField({ display_name: value });
            }}
          />
        ) : (
          <h2 className="cx-panel-name">{title}</h2>
        )}
      </div>

      {/* The three rows the approved design shows, and only these three. */}
      <div>
        <PanelField label="Phone">
          <span className="flex flex-col items-end gap-1">
            <PhoneNumberMenu
              e164={conversation.contact_e164}
              fromE164={conversation.our_e164}
              onText={focusComposer}
              disabled={!canSend}
              disabledReason="Read-only inbox — you can view but not call"
            />
            {otherPhones.map((phone) => (
              <PhoneNumberMenu
                key={phone.e164}
                e164={phone.e164}
                fromE164={conversation.our_e164}
                onText={focusComposer}
                disabled={!canSend}
                disabledReason="Read-only inbox — you can view but not call"
              />
            ))}
          </span>
        </PanelField>
        <PanelField label="Email">
          {contactId ? (
            <EditableField
              label="Email"
              hideLabel
              align="end"
              emptyText={<EmptyValue />}
              value={email}
              type="email"
              onSave={async (value) => {
                await saveAttribute("email", value);
              }}
            />
          ) : (
            <EmptyValue />
          )}
        </PanelField>
        <PanelField label="Company">
          {contactId ? (
            <EditableField
              label="Company"
              hideLabel
              align="end"
              emptyText={<EmptyValue />}
              value={company}
              onSave={async (value) => {
                await saveAttribute("company", value);
              }}
            />
          ) : (
            <EmptyValue />
          )}
        </PanelField>
      </div>

      {!contactId ? (
        <p className="mt-4 rounded-md border border-border bg-background p-3 text-xs text-muted-foreground">
          This number isn’t saved as a contact yet.
        </p>
      ) : (
        // NOT part of the approved design, and kept only because this panel is the sole
        // UI for any of it: /contacts/:contactId renders phones, duplicates, export and
        // erase, but never Role, Address, tags or the note list. Folded away so the panel
        // at rest reads as the four things it is supposed to be.
        <div className="mt-4 space-y-3">
          {/* Closed by default: at rest the panel is the four things the design asks
              for, and these are one click away rather than gone. */}
          <Collapsible
            storageKey="contact-panel.details"
            title="Details"
            defaultOpen={false}
          >
            <div className="space-y-4">
              <EditableField
                label="Role"
                value={role}
                onSave={async (value) => {
                  await saveAttribute("role", value);
                }}
              />
              <EditableField
                label="Address"
                value={address}
                onSave={async (value) => {
                  await saveAttribute("address", value);
                }}
              />
              {tagsToRender && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="cx-label">Tags</span>
                  {tagsToRender.map((tag) => (
                    <Pill key={tag} tone="neutral">
                      {tag}
                    </Pill>
                  ))}
                </div>
              )}
            </div>
          </Collapsible>

          <Collapsible
            storageKey="contact-panel.notes"
            title="Notes"
            defaultOpen={false}
          >
            <div className="space-y-2">
              {notesQuery.isLoading ? (
                <p className="text-xs text-muted-foreground">Loading notes…</p>
              ) : notesQuery.isError ? (
                <p role="alert" className="text-[11px] text-destructive">
                  {(notesQuery.error as Error).message}
                </p>
              ) : (notesQuery.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">No notes yet.</p>
              ) : (
                <ul aria-label="Notes" className="space-y-2">
                  {(notesQuery.data ?? []).map((note) => (
                    <li
                      key={note.id}
                      className="rounded-md border border-border bg-background p-2 text-xs text-foreground"
                    >
                      <p className="whitespace-pre-wrap break-words">{note.body}</p>
                      <p className="mt-1 text-[10px] text-muted-foreground">
                        {relativeTime(note.created_at)}
                      </p>
                    </li>
                  ))}
                </ul>
              )}

              <form
                className="space-y-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  const body = newNote.trim();
                  if (body && contactId) addNoteMutation.mutate(body);
                }}
              >
                <textarea
                  aria-label="Add note"
                  value={newNote}
                  onChange={(e) => setNewNote(e.target.value)}
                  placeholder="Add a note…"
                  rows={3}
                  disabled={addNoteMutation.isPending}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-muted-foreground"
                />
                <div className="flex items-center gap-2">
                  <Button
                    type="submit"
                    variant="outline"
                    size="sm"
                    disabled={!newNote.trim() || addNoteMutation.isPending}
                    className="text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
                  >
                    Add note
                  </Button>
                  {addNoteMutation.isPending && (
                    <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" /> Saving…
                    </span>
                  )}
                </div>
                {addNoteMutation.isError && (
                  <p role="alert" className="text-[11px] text-destructive">
                    {(addNoteMutation.error as Error).message}
                  </p>
                )}
              </form>
            </div>
          </Collapsible>
        </div>
      )}
    </aside>
  );
}
