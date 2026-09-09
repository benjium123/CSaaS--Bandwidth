import * as React from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, MessageSquare, Phone, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import {
  addContactNote,
  fetchContact,
  fetchContactNotes,
  fetchDepartments,
  fetchInboxGrants,
  fetchOrgMembers,
  updateContact,
  updateContactAttributes,
  type Contact,
  type Conversation,
  type Inbox,
} from "@/api/conversations";
import { Button, Collapsible, Input, Pill } from "@/components/ui/primitives";
import { PhoneNumberMenu } from "@/components/ui/PhoneNumberMenu";
import { formatPhone, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

function initialsFor(value: string): string {
  const match = value.match(/[A-Za-z0-9]/g);
  if (!match || match.length === 0) return "?";
  return match.slice(0, 2).join("").toUpperCase();
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
}: {
  label: string;
  value: string;
  onSave: (value: string) => Promise<void>;
  type?: string;
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

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <p className="text-[11px] font-medium uppercase tracking-wider text-neutral-500">
          {label}
        </p>
        {status === "saving" && (
          <span className="flex items-center gap-1 text-[10px] text-neutral-500">
            <Loader2 className="h-3 w-3 animate-spin" /> Saving…
          </span>
        )}
        {status === "saved" && (
          <span className="text-[10px] text-emerald-400">Saved</span>
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
            className="h-7 w-7 shrink-0 text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
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
            className="h-7 w-7 shrink-0 text-neutral-300 hover:bg-neutral-800"
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
          className="w-full justify-start rounded-md px-2 py-1 text-left text-xs text-neutral-200 hover:bg-neutral-800"
        >
          {value || "Add"}
        </Button>
      )}
      {status === "error" && error && (
        <p role="alert" className="text-[11px] text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}

export function ContactPanel({
  conversation,
  inbox,
  canSend = true,
  className,
}: {
  conversation: Conversation | null;
  inbox: Inbox | null;
  /** F2: viewers (my_role "viewer") can see the conversation but not act on it. */
  canSend?: boolean;
  className?: string;
}) {
  const { api } = useAuth();
  const softphone = useSoftphone();
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

  const grantsQuery = useQuery({
    queryKey: ["inbox-grants", inbox?.id],
    queryFn: () => fetchInboxGrants(api, inbox?.id as string),
    enabled: Boolean(inbox) && inbox?.my_role === "admin",
  });

  // T6: members/departments queries are enabled for everyone with a contact, not just
  // admins, so Owner/Team names resolve in the panel. The grants query above stays
  // admin-only because it authorizes sharing actions.
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
    enabled: Boolean(contactId) || inbox?.my_role === "admin",
  });
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
    // ...or when the admin-only "Shared with" block below needs member names for a
    // number that is not saved as a contact.
    enabled: Boolean(contactId) || inbox?.my_role === "admin",
  });

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
      <div
        className={cn(
          "flex h-full items-center justify-center bg-neutral-900 px-3 text-sm text-neutral-400",
          className,
        )}
      >
        No contact selected
      </div>
    );
  }

  const title =
    conversation.contact?.display_name ??
    formatPhone(conversation.contact_e164);

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

  async function startCall() {
    if (!conversation || !canSend) return;
    try {
      await softphone.dial(conversation.contact_e164, conversation.our_e164);
    } catch {
      /* softphone surface handles visible error */
    }
  }

  function focusComposer() {
    document
      .querySelector<HTMLInputElement>('input[aria-label="Message"]')
      ?.focus();
  }

  const departments = departmentsQuery.data ?? [];
  const members = membersQuery.data ?? [];
  const grants = grantsQuery.data ?? [];

  const ownerUserId = contactQuery.data?.owner_user_id ?? null;
  const departmentId = contactQuery.data?.department_id ?? null;

  // Members/departments errors intentionally do NOT render an error banner: the panel's
  // job is the contact, and "Unknown" already tells the truth when a value is set but
  // the directory cannot resolve it.
  const ownerLabel =
    ownerUserId == null
      ? "Unassigned"
      : membersQuery.isLoading
        ? "Loading…"
        : members.find((member) => member.user_id === ownerUserId)?.full_name ?? "Unknown";

  const teamLabel =
    departmentId == null
      ? "No team"
      : departmentsQuery.isLoading
        ? "Loading…"
        : departments.find((department) => department.id === departmentId)?.name ?? "Unknown";

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

  return (
    <aside
      className={cn(
        "h-full overflow-y-auto border-l border-neutral-800 bg-neutral-900 px-3 py-3 text-neutral-100",
        className,
      )}
      aria-label="Contact panel"
    >
      <div className="flex flex-col items-center text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-neutral-700 text-lg font-semibold text-neutral-100">
          {initialsFor(title)}
        </div>
        {contactId ? (
          <div className="mt-2 w-full">
            <EditableField
              label="Name"
              value={name}
              onSave={async (value) => {
                await saveField({ display_name: value });
              }}
            />
          </div>
        ) : (
          <h2 className="mt-2 text-sm font-semibold text-neutral-50">{title}</h2>
        )}

        <div className="mt-2 flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={startCall}
            disabled={!canSend}
            title={canSend ? undefined : "Read-only inbox — you can view but not call"}
            aria-label={`Call ${title}`}
            className="text-neutral-300 hover:bg-neutral-800 disabled:pointer-events-none disabled:opacity-40"
          >
            <Phone className="h-4 w-4" />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={focusComposer}
            aria-label={`Message ${title}`}
            className="text-neutral-300 hover:bg-neutral-800"
          >
            <MessageSquare className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {!contactId ? (
        <p className="mt-4 rounded-md border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-400">
          This number isn’t saved as a contact yet.
        </p>
      ) : (
        <>
          <div className="mt-4 space-y-1 border-t border-neutral-800 pt-3 text-xs text-neutral-200">
            <p>
              Owner: <span className="text-neutral-400">{ownerLabel}</span>
            </p>
            <p>
              Team: <span className="text-neutral-400">{teamLabel}</span>
            </p>
            {tagsToRender && (
              <div className="flex flex-wrap items-center gap-1 pt-1">
                <span className="text-neutral-500">Tags:</span>
                {tagsToRender.map((tag) => (
                  <Pill key={tag} tone="neutral">
                    {tag}
                  </Pill>
                ))}
              </div>
            )}
          </div>

          <div className="mt-3 flex justify-start">
            {/* P22 rule: this link can 404 when the contact-visibility policy excludes
                the user. That is correct - the panel still shows the conversation, but
                the contact record itself is not visible to them. */}
            <Link
              to={`/contacts/${contactId}`}
              className="inline-flex h-8 items-center justify-center gap-2 rounded-md px-3 text-xs font-medium text-neutral-200 hover:bg-neutral-800"
            >
              Open contact
            </Link>
          </div>

          <div className="mt-4 space-y-3">
            <Collapsible storageKey="contact-panel.details" title="Details">
              <div className="space-y-4">
                <EditableField
                  label="Company"
                  value={company}
                  onSave={async (value) => {
                    await saveAttribute("company", value);
                  }}
                />
                <EditableField
                  label="Role"
                  value={role}
                  onSave={async (value) => {
                    await saveAttribute("role", value);
                  }}
                />
                <div className="space-y-1">
                  <p className="text-[11px] font-medium uppercase tracking-wider text-neutral-500">
                    Phone
                  </p>
                  <div className="space-y-1">
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
                  </div>
                </div>
                <EditableField
                  label="Email"
                  value={email}
                  type="email"
                  onSave={async (value) => {
                    await saveAttribute("email", value);
                  }}
                />
                <EditableField
                  label="Address"
                  value={address}
                  onSave={async (value) => {
                    await saveAttribute("address", value);
                  }}
                />
              </div>
            </Collapsible>

            <Collapsible storageKey="contact-panel.notes" title="Notes">
              <div className="space-y-2">
                {notesQuery.isLoading ? (
                  <p className="text-xs text-neutral-400">Loading notes…</p>
                ) : notesQuery.isError ? (
                  <p role="alert" className="text-[11px] text-red-400">
                    {(notesQuery.error as Error).message}
                  </p>
                ) : (notesQuery.data ?? []).length === 0 ? (
                  <p className="text-xs text-neutral-400">No notes yet.</p>
                ) : (
                  <ul aria-label="Notes" className="space-y-2">
                    {(notesQuery.data ?? []).map((note) => (
                      <li
                        key={note.id}
                        className="rounded-md border border-neutral-800 bg-neutral-950 p-2 text-xs text-neutral-200"
                      >
                        <p className="whitespace-pre-wrap break-words">{note.body}</p>
                        <p className="mt-1 text-[10px] text-neutral-500">
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
                    className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
                  />
                  <div className="flex items-center gap-2">
                    <Button
                      type="submit"
                      variant="outline"
                      size="sm"
                      disabled={!newNote.trim() || addNoteMutation.isPending}
                      className="text-neutral-200 hover:bg-neutral-800 disabled:pointer-events-none disabled:opacity-50"
                    >
                      Add note
                    </Button>
                    {addNoteMutation.isPending && (
                      <span className="flex items-center gap-1 text-[10px] text-neutral-500">
                        <Loader2 className="h-3 w-3 animate-spin" /> Saving…
                      </span>
                    )}
                  </div>
                  {addNoteMutation.isError && (
                    <p role="alert" className="text-[11px] text-red-400">
                      {(addNoteMutation.error as Error).message}
                    </p>
                  )}
                </form>
              </div>
            </Collapsible>
          </div>
        </>
      )}

      {/* P20b: "Shared with" is about the INBOX, not the contact, so it stays OUTSIDE
          the contactId branch - an admin looking at a number that is not saved as a
          contact must still see who else can reach this inbox. */}
        {inbox?.my_role === "admin" && (
          <Collapsible storageKey="contact-panel.sharing" title="Shared with">
            <div className="space-y-2">
              {grants.length === 0 ? (
                <p className="text-xs text-neutral-400">
                  No one else has access to this inbox.
                </p>
              ) : (
                <ul className="space-y-1">
                  {grants.map((grant) => {
                    const label =
                      grant.grantee_type === "department"
                        ? departments.find((d) => d.id === grant.grantee_id)?.name ??
                          grant.grantee_id
                        : members.find((m) => m.user_id === grant.grantee_id)?.full_name ??
                          grant.grantee_id;
                    return (
                      <li
                        key={`${grant.grantee_type}-${grant.grantee_id}`}
                        className="flex items-center justify-between rounded-md bg-neutral-950 px-2 py-1 text-xs text-neutral-200"
                      >
                        <span>{label}</span>
                        <span className="text-neutral-500">{grant.role}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </Collapsible>
        )}
    </aside>
  );
}
