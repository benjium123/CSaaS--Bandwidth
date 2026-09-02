import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, MessageSquare, Phone, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import {
  fetchContact,
  fetchDepartments,
  fetchInboxGrants,
  fetchOrgMembers,
  updateContact,
  updateContactAttributes,
  type Contact,
  type Conversation,
  type Inbox,
} from "@/api/conversations";
import { formatPhone } from "@/lib/format";
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
          <input
            aria-label={label}
            type={type}
            value={draft}
            disabled={status === "saving"}
            onChange={(e) => setDraft(e.target.value)}
            className="h-8 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100 focus:outline-none focus:ring-1 focus:ring-neutral-500"
          />
          <button
            type="submit"
            disabled={status === "saving"}
            aria-label={`Save ${label}`}
            className="rounded-md p-1.5 text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
          >
            <Check className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(value);
              setStatus("idle");
              setError(null);
              setEditing(false);
            }}
            aria-label={`Cancel ${label}`}
            className="rounded-md p-1.5 text-neutral-300 hover:bg-neutral-800"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </form>
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="w-full rounded-md px-2 py-1 text-left text-xs text-neutral-200 hover:bg-neutral-800"
        >
          {value || "Add"}
        </button>
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
  const [notes, setNotes] = React.useState(contactQuery.data?.notes ?? "");

  React.useEffect(() => {
    const attrs = contactQuery.data?.attributes ?? {};
    setName(contactQuery.data?.display_name ?? conversation?.contact?.display_name ?? "");
    setCompany(attrText(attrs.company));
    setRole(attrText(attrs.role));
    setEmail(attrText(attrs.email));
    setAddress(attrText(attrs.address));
    setNotes(contactQuery.data?.notes ?? "");
  }, [contactQuery.data, conversation]);

  const grantsQuery = useQuery({
    queryKey: ["inbox-grants", inbox?.id],
    queryFn: () => fetchInboxGrants(api, inbox?.id as string),
    enabled: Boolean(inbox) && inbox?.my_role === "admin",
  });
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
    enabled: Boolean(inbox) && inbox?.my_role === "admin",
  });
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
    enabled: Boolean(inbox) && inbox?.my_role === "admin",
  });

  // F14: visible pending/error state instead of a bare fire-and-forget onBlur save.
  // Declared before the early return below - conditionally skipping a hook call would
  // violate the Rules of Hooks the moment `conversation` toggles between null and set.
  const notesMutation = useMutation({
    mutationFn: (nextNotes: string) => {
      if (!contactId) throw new Error("No contact to save notes for");
      return updateContact(api, contactId, { notes: nextNotes });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["contact", contactId] });
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
          <button
            type="button"
            onClick={startCall}
            disabled={!canSend}
            title={canSend ? undefined : "Read-only inbox — you can view but not call"}
            aria-label={`Call ${title}`}
            className="rounded-md border border-neutral-700 p-2 text-neutral-300 hover:bg-neutral-800 disabled:pointer-events-none disabled:opacity-40"
          >
            <Phone className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={focusComposer}
            aria-label={`Message ${title}`}
            className="rounded-md border border-neutral-700 p-2 text-neutral-300 hover:bg-neutral-800"
          >
            <MessageSquare className="h-4 w-4" />
          </button>
        </div>
      </div>

      {!contactId ? (
        <p className="mt-4 rounded-md border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-400">
          This number isn’t saved as a contact yet.
        </p>
      ) : (
        <div className="mt-5 space-y-4">
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
            <p className="px-2 py-1 text-xs text-neutral-200">
              {formatPhone(conversation.contact_e164)}
            </p>
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

          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <p className="text-[11px] font-medium uppercase tracking-wider text-neutral-500">
                Notes
              </p>
              {notesMutation.isPending && (
                <span className="flex items-center gap-1 text-[10px] text-neutral-500">
                  <Loader2 className="h-3 w-3 animate-spin" /> Saving…
                </span>
              )}
              {notesMutation.isSuccess && !notesMutation.isPending && (
                <span className="text-[10px] text-emerald-400">Saved</span>
              )}
            </div>
            <textarea
              aria-label="Notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={() => {
                if (contactId) notesMutation.mutate(notes);
              }}
              rows={4}
              className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
            />
            {notesMutation.isError && (
              <p role="alert" className="text-[11px] text-red-400">
                {(notesMutation.error as Error).message}
              </p>
            )}
          </div>
        </div>
      )}

      {inbox?.my_role === "admin" && (
        <div className="mt-5 space-y-2 border-t border-neutral-800 pt-4">
          <p className="text-[11px] font-medium uppercase tracking-wider text-neutral-500">
            Shared with
          </p>
          {grants.length === 0 ? (
            <p className="text-xs text-neutral-400">No one else has access to this inbox.</p>
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
      )}
    </aside>
  );
}
